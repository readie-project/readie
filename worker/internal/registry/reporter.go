// Package registry reports worker and executor state to the router.
//
// Every report is best-effort: the router losing a status update degrades
// scheduling quality but must never fail an in-flight execution. Callers log
// the returned error and continue.
package registry

import (
	"context"
	"errors"

	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

// Sentinel errors returned by a Reporter.
var (
	// ErrNotUpdated indicates the router accepted the call but declined to
	// record it.
	ErrNotUpdated = errors.New("router did not record the update")
	// ErrRouterUnavailable indicates the router could not be reached.
	ErrRouterUnavailable = errors.New("router unavailable")
)

// ExecutionRef identifies the request an operation belongs to.
//
// It replaces the previous implementation's untyped-string context key, whose
// unchecked type assertion panicked whenever a caller forgot to seed the
// context. Passing the identifiers explicitly makes that impossible.
type ExecutionRef struct {
	RequestID string
	SessionID string
}

// Utilization is a resource usage report.
//
// The field names mirror the router's registry.proto. Note that the router
// treats cpu_util/cpu_total as a generic pair; see the container package for
// what the worker actually puts in them.
type Utilization struct {
	CPUUtil  int64
	CPUTotal int64
	GPUUtil  int64
	GPUTotal int64
}

// Reporter posts worker and executor state to the router.
type Reporter interface {
	WorkerStatus(ctx context.Context, status pb.Status) error
	ExecutorStatus(ctx context.Context, ref ExecutionRef, containerID string, status pb.Status) error
	WorkerUtilization(ctx context.Context, u Utilization) error
	ExecutorUtilization(ctx context.Context, containerID string, u Utilization) error
}

// NopReporter discards every report. It is the default for tests and for
// running the worker without a router.
type NopReporter struct{}

// NewNopReporter returns a Reporter that does nothing.
func NewNopReporter() NopReporter { return NopReporter{} }

// WorkerStatus does nothing.
func (NopReporter) WorkerStatus(context.Context, pb.Status) error { return nil }

// ExecutorStatus does nothing.
func (NopReporter) ExecutorStatus(context.Context, ExecutionRef, string, pb.Status) error {
	return nil
}

// WorkerUtilization does nothing.
func (NopReporter) WorkerUtilization(context.Context, Utilization) error { return nil }

// ExecutorUtilization does nothing.
func (NopReporter) ExecutorUtilization(context.Context, string, Utilization) error { return nil }

var _ Reporter = NopReporter{}
