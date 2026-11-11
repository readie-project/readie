package execution_test

import (
	"context"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/executor"
)

// A trailer read fails immediately, making the regression deterministic rather
// than depending on the telemetry timeout or warm-call latency measurements.
type resultOnlyConn struct {
	executor.Conn
	remaining    int
	trailerReads int
}

func (c *resultOnlyConn) Read(p []byte) (int, error) {
	if c.remaining == 0 {
		c.trailerReads++
		return 0, io.ErrUnexpectedEOF
	}
	n, err := c.Conn.Read(p)
	c.remaining -= n
	return n, err
}

func TestTracing_UnrecordedOrUnsampledAttemptsSkipTheTrailer(t *testing.T) {
	for _, mode := range []string{"disabled", "unsampled"} {
		t.Run(mode, func(t *testing.T) {
			var conn *resultOnlyConn
			h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
				if mode == "unsampled" {
					provider := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.NeverSample()))
					t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
					h.tracer = provider.Tracer("worker")
				}
				h.dialer.wrapConn = func(raw executor.Conn) executor.Conn {
					conn = &resultOnlyConn{Conn: raw, remaining: len(h.dialer.reply) + 2*executor.LengthBytes}
					return conn
				}
			})
			// A disabled tracer may inherit a sampled context without recording.
			ctx := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{
				TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled, Remote: true,
			}))
			_, err := h.runner.Run(ctx, request(), &chunkSource{}, h.sink)
			require.NoError(t, err)
			require.NoError(t, h.runner.WaitPendingReleases(context.Background()))
			require.NotNil(t, conn)
			assert.Zero(t, conn.trailerReads)
			assert.Equal(t, []container.Outcome{container.OutcomeSuccess}, h.containers.Outcomes())
		})
	}
}

func TestTracing_AttemptFailuresIdentifyTheFailingPhase(t *testing.T) {
	for _, phase := range []string{"container acquire", "executor dial"} {
		t.Run(phase, func(t *testing.T) {
			recorder := tracetest.NewSpanRecorder()
			provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
			t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
			failure := errors.New("phase failed")
			h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
				h.tracer = provider.Tracer("worker")
				if phase == "container acquire" {
					h.containers.acquireErr = failure
				} else {
					h.dialer.dialErr = failure
				}
			})
			_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
			require.ErrorIs(t, err, failure)
			require.NoError(t, h.runner.WaitPendingReleases(context.Background()))

			var failed []string
			for _, span := range recorder.Ended() {
				if span.Status().Code == codes.Error {
					failed = append(failed, span.Name())
					var exception bool
					for _, event := range span.Events() {
						exception = exception || event.Name == "exception"
					}
					assert.True(t, exception)
				}
			}
			assert.ElementsMatch(t, []string{phase, "execution attempt"}, failed)
		})
	}
}

func TestTracing_OOMRetryHasSeparateAttemptsAndAParentEvent(t *testing.T) {
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
	tracer := provider.Tracer("worker")
	ctx, parent := tracer.Start(context.Background(), "request")
	h := newHarness(t, execution.RunnerConfig{WorkerMemTotal: 2 << 30}, func(h *harness) {
		h.tracer = tracer
		h.dialer.noReply = true
	})
	_, err := h.runner.Run(ctx, request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	require.NoError(t, h.runner.WaitPendingReleases(context.Background()))
	parent.End()

	var allocations []int64
	for _, span := range recorder.Ended() {
		if span.Name() == "execution attempt" {
			assert.Equal(t, parent.SpanContext().SpanID(), span.Parent().SpanID())
			assert.Equal(t, codes.Error, span.Status().Code)
			attrs := attribute.NewSet(span.Attributes()...)
			alloc, _ := attrs.Value("mem_alloc")
			allocations = append(allocations, alloc.AsInt64())
		}
		if span.Name() == "request" {
			require.Len(t, span.Events(), 1)
			assert.Equal(t, "execution retrying after possible OOM", span.Events()[0].Name)
			assert.Equal(t, codes.Unset, span.Status().Code, "attempt errors do not directly mark the parent as failed")
		}
	}
	assert.Equal(t, []int64{512 << 20, 1 << 30}, allocations)
}

func TestTracing_ReleaseFailureDoesNotFailSuccessfulExecution(t *testing.T) {
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h := newHarness(t, execution.RunnerConfig{ReleaseTimeout: 10 * time.Millisecond}, func(h *harness) {
		h.tracer = provider.Tracer("worker")
		h.containers.releaseDelay = time.Hour
	})
	_, err := h.runner.Run(ctx, request(), &chunkSource{}, h.sink)
	require.NoError(t, err)
	cancel()
	require.NoError(t, h.runner.WaitPendingReleases(context.Background()))

	spans := recorder.Ended()
	var attempt, release sdktrace.ReadOnlySpan
	for _, span := range spans {
		switch span.Name() {
		case "execution attempt":
			attempt = span
		case "container release":
			release = span
		}
	}
	require.NotNil(t, attempt)
	require.NotNil(t, release)
	assert.Equal(t, codes.Unset, attempt.Status().Code)
	assert.Equal(t, attempt.SpanContext().SpanID(), release.Parent().SpanID())
	assert.Equal(t, attempt.SpanContext().TraceID(), release.SpanContext().TraceID())
	assert.Equal(t, codes.Error, release.Status().Code)
	require.NotEmpty(t, release.Events())
	assert.Equal(t, "exception", release.Events()[0].Name)
	attrs := attribute.NewSet(release.Events()[0].Attributes...)
	message, _ := attrs.Value("exception.message")
	assert.Equal(t, context.DeadlineExceeded.Error(), message.AsString(), "release is detached from request cancellation")
}
