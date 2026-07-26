// Package grpcserver adapts the worker's execution service to gRPC.
//
// It contains no orchestration logic: the handler translates a protobuf stream
// into the transport-agnostic ports the execution package defines, and
// translates errors back into status codes.
package grpcserver

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/artifact"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/execution"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// ErrInvalidRequest indicates the caller sent something unusable.
var ErrInvalidRequest = errors.New("invalid execution request")

// toStatus maps an internal error onto a gRPC status.
//
// Status messages stay generic on purpose: the specifics belong in the log line
// keyed by request_id, not on a wire the worker does not control. The one
// exception is a misconfigured worker, where the specifics are the whole point
// -- nobody debugging it is reading this worker's logs yet.
func toStatus(err error) error {
	switch {
	case err == nil:
		return nil

	case errors.Is(err, ErrInvalidRequest):
		return status.Error(codes.InvalidArgument, err.Error())

	case errors.Is(err, execution.ErrClientClosed), errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "request cancelled")

	case errors.Is(err, execution.ErrExecutionTimeout),
		errors.Is(err, executor.ErrNoResponse),
		errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "execution timed out")

	case errors.Is(err, sandbox.ErrNotFound):
		return status.Error(codes.NotFound, "container not found")

	case errors.Is(err, sandbox.ErrRuntimeUnavailable),
		errors.Is(err, executor.ErrDialTimeout),
		errors.Is(err, registry.ErrRouterUnavailable):
		return status.Error(codes.Unavailable, "worker temporarily unavailable")

	case errors.Is(err, artifact.ErrNoArtifacts), errors.Is(err, artifact.ErrIncompleteArtifacts):
		// FailedPrecondition, not ResourceExhausted: this worker is not out of
		// capacity, it has no root filesystem to run anything against. And the
		// message names the cause rather than staying generic, because no
		// amount of retrying or scaling fixes it -- the worker image was built
		// without artifacts, and the router would otherwise keep reporting a
		// capacity problem that does not exist.
		return status.Error(codes.FailedPrecondition,
			"worker has no root filesystem; rebuild its image with `make generation`")

	case errors.Is(err, container.ErrAcquireFailed):
		return status.Error(codes.ResourceExhausted, "could not provision a container")

	default:
		// An error carrying a status already (for example one propagated from
		// a client stream) keeps its code.
		if s, ok := status.FromError(err); ok && s.Code() != codes.Unknown {
			return err
		}
		return status.Error(codes.Internal, "internal error")
	}
}
