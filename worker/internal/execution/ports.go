// Package execution orchestrates a single request end to end: acquiring a
// container, streaming the request body to its executor, and relaying the
// response, logs and resource samples back.
//
// Nothing here depends on gRPC. The transport supplies a PayloadSource and a
// Sink, which is what lets the whole choreography — including its concurrency
// and timeout behaviour — be tested in-process under the race detector.
package execution

import (
	"context"
	"errors"
	"io"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/registry"
	"github.com/illinoisdata/readie/worker/internal/sandbox"
)

// Sentinel errors returned by a Runner.
var (
	// ErrExecutionTimeout indicates the execution exceeded its deadline.
	//
	// The previous implementation raised a panic here and recovered it in a
	// deferred function that discarded the error, so a timed-out execution was
	// reported to the router as a success.
	ErrExecutionTimeout = errors.New("execution timed out")

	// ErrClientClosed indicates the caller went away mid-execution.
	ErrClientClosed = errors.New("client closed the request")
)

// ContainerService is the container lifecycle this package needs.
// *container.Manager satisfies it.
type ContainerService interface {
	Acquire(ctx context.Context, req container.AcquireRequest) (container.Handle, error)
	Release(ctx context.Context, ref registry.ExecutionRef, h container.Handle, outcome container.Outcome) error
	Logs(ctx context.Context, id string) (io.ReadCloser, error)
	Stats(ctx context.Context, id string) (sandbox.StatsStream, error)
	// Grow raises a live container's memory limit, for proactive auto-expand.
	Grow(ctx context.Context, id string, memBytes int64) error
}

// PayloadSource yields request-body chunks after the first message.
//
// It returns io.EOF once the caller has finished sending, which is the normal
// terminator rather than a failure.
type PayloadSource interface {
	Next(ctx context.Context) ([]byte, error)
}

// ProvisionInfo is the scheduling decision the caller must learn about.
//
// The router reads these fields off the first response it receives and uses
// them to update its own registry, so they must reach it before any output.
type ProvisionInfo struct {
	Ref          registry.ExecutionRef
	WorkerID     string
	ContainerID  string
	CheckpointID string
	Budgets      []container.Budget
}

// Sink receives an execution's output.
//
// Contract: Provision is called exactly once, before any Payload or Logs call,
// and every method is invoked from the single goroutine that called Run.
// Implementations therefore need no locking — which is the point, since gRPC
// streams are not safe for concurrent sends.
type Sink interface {
	Provision(info ProvisionInfo) error
	Payload(p []byte) error
	Logs(msg string) error
}

// Request is one execution request.
type Request struct {
	Ref registry.ExecutionRef
	// ContainerID names a warm container to reuse. Empty provisions a new one.
	ContainerID  string
	CheckpointID string
	Alloc        container.Allocation
	// InitialPayload is the body carried by the first message.
	InitialPayload []byte
}

func (r Request) toAcquire() container.AcquireRequest {
	return container.AcquireRequest{
		Ref:          r.Ref,
		ContainerID:  r.ContainerID,
		CheckpointID: r.CheckpointID,
		Alloc:        r.Alloc,
	}
}

// Result describes a completed execution.
type Result struct {
	Handle container.Handle
	// BytesSent is the size of the response body relayed to the caller.
	BytesSent int
}
