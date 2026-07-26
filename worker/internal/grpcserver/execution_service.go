package grpcserver

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/execution"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

// Runner executes a request. *execution.Runner satisfies it.
type Runner interface {
	Run(ctx context.Context, req execution.Request, src execution.PayloadSource, sink execution.Sink) (execution.Result, error)
}

// ExecutionService serves the ExecutionService gRPC contract.
//
// It is deliberately thin: receive the first message, adapt the stream, and
// hand off. Everything that used to make this handler untestable — the
// goroutine choreography, the timeout handling, the container lifecycle —
// lives in the execution package, behind interfaces.
type ExecutionService struct {
	pb.UnimplementedExecutionServiceServer

	runner Runner
	log    *slog.Logger
}

var _ pb.ExecutionServiceServer = (*ExecutionService)(nil)

// NewExecutionService builds the handler.
func NewExecutionService(runner Runner, log *slog.Logger) *ExecutionService {
	if log == nil {
		log = slog.Default()
	}
	return &ExecutionService{runner: runner, log: log}
}

// RequestExecution runs one execution against this worker.
func (s *ExecutionService) RequestExecution(stream executionStream) error {
	// The stream's own context is used, so a caller that hangs up actually
	// cancels the work. The previous implementation started from
	// context.Background and never learned about disconnections.
	ctx := stream.Context()

	first, err := stream.Recv()
	switch {
	case errors.Is(err, io.EOF):
		s.log.Warn("execution request stream closed before sending anything")
		return toStatus(fmt.Errorf("%w: empty request stream", ErrInvalidRequest))
	case err != nil:
		return toStatus(fmt.Errorf("receive first message: %w", err))
	}

	req, err := toExecutionRequest(first)
	if err != nil {
		return toStatus(err)
	}

	log := s.log.With(
		logging.KeyRequestID, req.Ref.RequestID,
		logging.KeySessionID, req.Ref.SessionID,
	)
	log.Info("execution requested",
		logging.KeyContainerID, req.ContainerID,
		logging.KeyCheckpoint, req.CheckpointID,
		"cpu_alloc", req.Alloc.CPUAlloc)

	// SerialSink is belt-and-braces around the runner's single-owner contract:
	// concurrent sends on a gRPC stream corrupt frames rather than failing.
	sink := execution.NewSerialSink(&streamSink{stream: stream})

	result, err := s.runner.Run(ctx, req, streamSource{stream: stream}, sink)
	if err != nil {
		log.Error("execution failed", logging.KeyError, err)
		return toStatus(err)
	}

	log.Info("execution completed",
		logging.KeyContainerID, result.Handle.ID,
		"bytes_sent", result.BytesSent)
	return nil
}
