package integration

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakeexecutor"
	pb "github.com/illinoisdata/readie/worker/proto"
)

func executeToEOF(t *testing.T, h *harness, containerID string) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := h.Exec.RequestExecution(ctx)
	require.NoError(t, err)
	req := &pb.WorkerExecutionRequest{RequestId: "telemetry-test", SessionId: "session", Payload: []byte("private payload")}
	if containerID != "" {
		req.ContainerId = &containerID
	}
	require.NoError(t, stream.Send(req))
	require.NoError(t, stream.CloseSend())
	var payload []byte
	for {
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		require.NoError(t, err)
		containerID = msg.GetContainerId()
		payload = append(payload, msg.GetPayload()...)
	}
	assert.Equal(t, "executor-result", string(payload))
	return containerID
}

// The trailer cannot even be sent until the client has seen successful EOF.
// Waiting for it anywhere in the RPC path would deadlock this test.
func TestTelemetry_RPCCompletesBeforeTheExecutorSendsItsTrailer(t *testing.T) {
	gate := make(chan struct{})
	provider := sdktrace.NewTracerProvider()
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
	h := newHarness(t, withExecutor(fakeexecutor.Options{Reply: []byte("executor-result"), TelemetryGate: gate}),
		func(o *harnessOptions) { o.tracer = provider.Tracer("worker") })
	id := executeToEOF(t, h, "")
	close(gate)
	// Immediate reuse must wait out the old finalizer, not race its READY report.
	executeToEOF(t, h, id)
	require.NoError(t, h.Shutdown())
}

func TestTelemetry_InvalidOrStalledTrailersDoNotFailDeliveredResults(t *testing.T) {
	for _, name := range []string{"invalid", "stalled"} {
		t.Run(name, func(t *testing.T) {
			opts := fakeexecutor.Options{Reply: []byte("executor-result")}
			if name == "invalid" {
				opts.Telemetry = []byte(`{"private":"invalid"}`)
			} else {
				opts.TelemetryGate = make(chan struct{})
			}
			recorder := tracetest.NewSpanRecorder()
			provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
			t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
			h := newHarness(t, withExecutor(opts), func(o *harnessOptions) { o.tracer = provider.Tracer("worker") })
			executeToEOF(t, h, "")
			require.NoError(t, h.Shutdown())
			for _, span := range recorder.Ended() {
				assert.NotEqual(t, "executor execution", span.Name())
				assert.Equal(t, codes.Unset, span.Status().Code)
			}
		})
	}
}

func TestTelemetry_FunctionFailureDoesNotFailTheWorkerTransportSpan(t *testing.T) {
	data, err := json.Marshal(executor.Telemetry{
		StartTimeUnixNS: time.Now().UnixNano(), DurationNS: 100, Outcome: "invoke_error",
		Phases: []executor.Phase{{Name: "invoke", DurationNS: 100, Outcome: "invoke_error"}},
	})
	require.NoError(t, err)
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
	h := newHarness(t, withExecutor(fakeexecutor.Options{Reply: []byte("executor-result"), Telemetry: data}),
		func(o *harnessOptions) { o.tracer = provider.Tracer("worker") })
	executeToEOF(t, h, "")
	require.NoError(t, h.Shutdown())
	var failed []string
	for _, span := range recorder.Ended() {
		if span.Status().Code == codes.Error {
			failed = append(failed, span.Name())
		}
	}
	assert.ElementsMatch(t, []string{"executor execution", "executor invoke"}, failed)
}
