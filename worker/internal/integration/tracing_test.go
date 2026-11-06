package integration

import (
	"context"
	"errors"
	"fmt"
	"io"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/metadata"

	pb "github.com/illinoisdata/readie/worker/proto"
)

func TestTracing_InjectedTracerFollowsRequestThroughWorkerAndRelease(t *testing.T) {
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
	h := newHarness(t, func(o *harnessOptions) { o.tracer = provider.Tracer("injected-worker") })

	ctx := metadata.NewOutgoingContext(context.Background(), metadata.Pairs(
		"traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
	))
	stream, err := h.Exec.RequestExecution(ctx)
	require.NoError(t, err)
	require.NoError(t, stream.Send(&pb.WorkerExecutionRequest{
		RequestId: "req-traced", SessionId: "session-traced", Payload: []byte("private request"),
	}))
	require.NoError(t, stream.CloseSend())
	var payload []byte
	for {
		msg, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		require.NoError(t, err)
		payload = append(payload, msg.GetPayload()...)
	}
	assert.Equal(t, "executor-result", string(payload))
	// Shutdown drains background releases before their spans are inspected.
	require.NoError(t, h.Shutdown())

	byName := make(map[string]sdktrace.ReadOnlySpan)
	for _, span := range recorder.Ended() {
		byName[span.Name()] = span
		assert.Equal(t, "injected-worker", span.InstrumentationScope().Name)
		assert.Equal(t, "4bf92f3577b34da6a3ce929d0e0e4736", span.SpanContext().TraceID().String())
		assert.Equal(t, codes.Unset, span.Status().Code)
		assert.NotContains(t, fmt.Sprint(span.Attributes(), span.Events()), "private request")
	}
	require.Len(t, byName, 5)
	server := byName["ExecutionService/RequestExecution"]
	attempt := byName["execution attempt"]
	require.NotNil(t, server)
	require.NotNil(t, attempt)
	assert.Equal(t, trace.SpanKindServer, server.SpanKind())
	assert.Equal(t, "00f067aa0ba902b7", server.Parent().SpanID().String())
	assert.Equal(t, server.SpanContext().SpanID(), attempt.Parent().SpanID())
	for _, name := range []string{"container acquire", "executor dial", "container release"} {
		span := byName[name]
		require.NotNil(t, span, name)
		assert.Equal(t, attempt.SpanContext().SpanID(), span.Parent().SpanID(), name)
	}
	attrs := attribute.NewSet(server.Attributes()...)
	id, _ := attrs.Value("request_id")
	assert.Equal(t, "req-traced", id.AsString())
	var events []string
	for _, ev := range attempt.Events() {
		events = append(events, ev.Name)
	}
	assert.Equal(t, []string{"container acquired", "executor connected", "response started"}, events)
}
