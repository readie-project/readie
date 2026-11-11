package grpcserver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/logging"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// Runner executes a request. *execution.Runner satisfies it.
type Runner interface {
	Run(ctx context.Context, req execution.Request, src execution.PayloadSource, sink execution.Sink) (execution.Result, error)
}

// ExecutionService serves the ExecutionService gRPC contract.
//
// It is deliberately thin: receive the first message, adapt the stream, and
// hand off. Everything that used to make this handler untestable - the
// goroutine choreography, the timeout handling, the container lifecycle -
// lives in the execution package, behind interfaces.
type ExecutionService struct {
	pb.UnimplementedExecutionServiceServer

	runner Runner
	log    *slog.Logger
	trace  trace.Tracer
}

var _ pb.ExecutionServiceServer = (*ExecutionService)(nil)

// NewExecutionService builds the handler.
func NewExecutionService(runner Runner, log *slog.Logger, tracer trace.Tracer) *ExecutionService {
	if log == nil {
		log = slog.Default()
	}
	if tracer == nil {
		tracer = noop.NewTracerProvider().Tracer("worker")
	}
	return &ExecutionService{runner: runner, log: log, trace: tracer}
}

type metadataCarrier metadata.MD

func (c metadataCarrier) Get(key string) string {
	values := metadata.MD(c).Get(key)
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

func (c metadataCarrier) Set(key, value string) { metadata.MD(c).Set(key, value) }

func (c metadataCarrier) Keys() []string {
	keys := make([]string, 0, len(c))
	for key := range c {
		keys = append(keys, key)
	}
	return keys
}

// RequestExecution runs one execution against this worker.
func (s *ExecutionService) RequestExecution(stream executionStream) error {
	// The stream's own context is used, so a caller that hangs up actually
	// cancels the work. The previous implementation started from
	// context.Background and never learned about disconnections.
	ctx := stream.Context()

	md, _ := metadata.FromIncomingContext(ctx)
	ctx = propagation.TraceContext{}.Extract(ctx, metadataCarrier(md))
	ctx, span := s.trace.Start(ctx, "ExecutionService/RequestExecution",
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			attribute.String("rpc.system", "grpc"),
			attribute.String("rpc.service", "ExecutionService"),
			attribute.String("rpc.method", "RequestExecution"),
			attribute.Int("rpc.grpc.status_code", 0),
		))
	defer span.End()

	fail := func(err error) error {
		rpcErr := toStatus(err)
		span.RecordError(err)
		span.SetStatus(codes.Error, status.Convert(rpcErr).Message())
		span.SetAttributes(attribute.Int("rpc.grpc.status_code", int(status.Code(rpcErr))))
		span.AddEvent("execution failed")
		return rpcErr
	}

	first, err := stream.Recv()
	switch {
	case errors.Is(err, io.EOF):
		s.log.Warn("execution request stream closed before sending anything")
		return fail(fmt.Errorf("%w: empty request stream", ErrInvalidRequest))
	case err != nil:
		return fail(fmt.Errorf("receive first message: %w", err))
	}

	req, err := toExecutionRequest(first)
	if err != nil {
		return fail(err)
	}

	span.SetAttributes(
		attribute.String(logging.KeyRequestID, req.Ref.RequestID),
		attribute.String(logging.KeySessionID, req.Ref.SessionID),
		attribute.String("requested_container_id", req.ContainerID),
		attribute.String("requested_checkpoint_id", req.CheckpointID),
		attribute.Int64("mem_alloc", req.Alloc.Memory().Alloc),
	)
	span.AddEvent("execution requested")

	log := s.log.With(
		logging.KeyRequestID, req.Ref.RequestID,
		logging.KeySessionID, req.Ref.SessionID,
	)
	log.Info("execution requested",
		logging.KeyContainerID, req.ContainerID,
		logging.KeyCheckpoint, req.CheckpointID,
		"mem_alloc", req.Alloc.Memory().Alloc)

	// SerialSink is belt-and-braces around the runner's single-owner contract:
	// concurrent sends on a gRPC stream corrupt frames rather than failing.
	sink := execution.NewSerialSink(&streamSink{stream: stream})

	req.ResponseDone, _ = ctx.Value(responseDoneKey{}).(chan struct{})
	result, err := s.runner.Run(ctx, req, streamSource{stream: stream}, sink)
	if err != nil {
		log.Error("execution failed", logging.KeyError, err)
		return fail(err)
	}

	span.SetAttributes(
		attribute.String(logging.KeyContainerID, result.Handle.ID),
		attribute.String(logging.KeyCheckpoint, result.Handle.CheckpointID),
		attribute.Int("bytes_sent", result.BytesSent),
	)
	span.AddEvent("execution completed")

	log.Info("execution completed",
		logging.KeyContainerID, result.Handle.ID,
		"bytes_sent", result.BytesSent)
	return nil
}
