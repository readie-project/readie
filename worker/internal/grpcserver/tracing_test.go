package grpcserver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/logging"
	pb "github.com/illinoisdata/readie/worker/proto"
)

type runnerFunc func(context.Context, execution.Request, execution.PayloadSource, execution.Sink) (execution.Result, error)

func (f runnerFunc) Run(ctx context.Context, req execution.Request, src execution.PayloadSource, sink execution.Sink) (execution.Result, error) {
	return f(ctx, req, src, sink)
}

type headerStream struct {
	executionStream
	ctx    context.Context
	header *pb.WorkerExecutionRequest
	err    error
}

func (s *headerStream) Context() context.Context                  { return s.ctx }
func (s *headerStream) Recv() (*pb.WorkerExecutionRequest, error) { return s.header, s.err }

// Regression: http.Header canonicalization missed gRPC's lowercase traceparent
// metadata and silently started a new trace instead of continuing the router's.
func TestTracing_ContinuesRouterParentAndPassesSpanToRunner(t *testing.T) {
	recorder := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })

	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		"traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
		"tracestate", "vendor=opaque",
	))
	var runnerSpan trace.SpanContext
	runner := runnerFunc(func(ctx context.Context, _ execution.Request, _ execution.PayloadSource, _ execution.Sink) (execution.Result, error) {
		runnerSpan = trace.SpanContextFromContext(ctx)
		return execution.Result{Handle: container.Handle{ID: "actual-container"}, BytesSent: 12}, nil
	})
	svc := NewExecutionService(runner, logging.Discard(), provider.Tracer("injected-worker"))
	require.NoError(t, svc.RequestExecution(&headerStream{ctx: ctx, header: &pb.WorkerExecutionRequest{
		RequestId: "req-1", SessionId: "sess-1", CheckpointId: "requested-checkpoint",
		Payload: []byte("private payload must not be traced"),
	}}))

	spans := recorder.Ended()
	require.Len(t, spans, 1)
	span := spans[0]
	assert.Equal(t, "ExecutionService/RequestExecution", span.Name())
	assert.Equal(t, trace.SpanKindServer, span.SpanKind())
	assert.Equal(t, "injected-worker", span.InstrumentationScope().Name)
	assert.Equal(t, "4bf92f3577b34da6a3ce929d0e0e4736", span.SpanContext().TraceID().String())
	assert.Equal(t, "00f067aa0ba902b7", span.Parent().SpanID().String())
	assert.True(t, span.Parent().IsRemote())
	assert.Equal(t, "vendor=opaque", span.Parent().TraceState().String())
	assert.Equal(t, span.SpanContext(), runnerSpan)
	assert.Equal(t, otelcodes.Unset, span.Status().Code)
	attrs := attribute.NewSet(span.Attributes()...)
	assert.Equal(t, attribute.StringValue("req-1"), valueOf(attrs, "request_id"))
	assert.Equal(t, attribute.StringValue("sess-1"), valueOf(attrs, "session_id"))
	assert.Equal(t, attribute.StringValue("requested-checkpoint"), valueOf(attrs, "requested_checkpoint_id"))
	assert.Equal(t, attribute.StringValue("actual-container"), valueOf(attrs, "container_id"))
	assert.Equal(t, attribute.StringValue(""), valueOf(attrs, "checkpoint_id"), "a cold start must not claim the requested checkpoint")
	assert.Equal(t, attribute.IntValue(12), valueOf(attrs, "bytes_sent"))
	assert.Equal(t, attribute.IntValue(0), valueOf(attrs, "rpc.grpc.status_code"))
	assert.NotContains(t, fmt.Sprint(span.Attributes(), span.Events()), "private payload must not be traced")
	require.Len(t, span.Events(), 2)
	assert.Equal(t, "execution requested", span.Events()[0].Name)
	assert.Equal(t, "execution completed", span.Events()[1].Name)
}

func TestTracing_RecordsEveryHandlerFailureAndEndsSpan(t *testing.T) {
	tests := []struct {
		name    string
		header  *pb.WorkerExecutionRequest
		recvErr error
		runErr  error
		code    codes.Code
	}{
		{name: "empty stream", recvErr: io.EOF, code: codes.InvalidArgument},
		{name: "receive failure", recvErr: errors.New("broken stream"), code: codes.Internal},
		{name: "nil header", code: codes.InvalidArgument},
		{name: "runner failure", header: &pb.WorkerExecutionRequest{}, runErr: errors.New("runtime failed"), code: codes.Internal},
		{name: "cancellation", header: &pb.WorkerExecutionRequest{}, runErr: context.Canceled, code: codes.Canceled},
		{name: "timeout", header: &pb.WorkerExecutionRequest{}, runErr: execution.ErrExecutionTimeout, code: codes.DeadlineExceeded},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			recorder := tracetest.NewSpanRecorder()
			provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
			t.Cleanup(func() { require.NoError(t, provider.Shutdown(context.Background())) })
			runner := runnerFunc(func(context.Context, execution.Request, execution.PayloadSource, execution.Sink) (execution.Result, error) {
				return execution.Result{}, tt.runErr
			})
			svc := NewExecutionService(runner, logging.Discard(), provider.Tracer("test"))
			err := svc.RequestExecution(&headerStream{ctx: context.Background(), header: tt.header, err: tt.recvErr})
			assert.Equal(t, tt.code, status.Code(err))
			spans := recorder.Ended()
			require.Len(t, spans, 1)
			span := spans[0]
			assert.Equal(t, otelcodes.Error, span.Status().Code)
			attrs := attribute.NewSet(span.Attributes()...)
			assert.Equal(t, attribute.IntValue(int(tt.code)), valueOf(attrs, "rpc.grpc.status_code"))
			var exception bool
			for _, ev := range span.Events() {
				exception = exception || ev.Name == "exception"
			}
			assert.True(t, exception)
		})
	}
}

func TestTracing_NilTracerAndMalformedParentDoNotBreakExecution(t *testing.T) {
	runner := runnerFunc(func(context.Context, execution.Request, execution.PayloadSource, execution.Sink) (execution.Result, error) {
		return execution.Result{}, nil
	})
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("traceparent", "invalid"))
	svc := NewExecutionService(runner, logging.Discard(), nil)
	require.NoError(t, svc.RequestExecution(&headerStream{ctx: ctx, header: &pb.WorkerExecutionRequest{}}))
}

func valueOf(set attribute.Set, key attribute.Key) attribute.Value {
	value, _ := set.Value(key)
	return value
}
