// Package docker is the worker's sole boundary to a container runtime.
//
// The package exposes two layers. Port is a narrow, runtime-agnostic interface
// expressed in this package's own DTOs; every other package depends on it and
// only on it, which is what makes container orchestration testable without a
// daemon. MobyAdapter implements Port against github.com/moby/moby/client and
// is the only file in the repository that imports moby — the client is a
// pre-1.0 module whose signatures have already churned once, so confining it
// keeps the next churn to a single file.
package docker

import (
	"context"
	"io"
	"time"
)

// CreateSpec describes a container to create.
//
// The field values form a contract with the Python executor and the router:
// the executor discovers its socket directory through Env, and the bind mounts
// are what make that socket visible to the worker. See
// scripts/executor/app.py and the project README before changing any of them.
type CreateSpec struct {
	// Name is the container name, which the worker also uses as the container
	// identifier in every downstream call and log line.
	Name string

	Image string

	// Env must carry EXECUTOR_DIR and PYTHONPATH; the executor exits non-zero
	// without the former.
	Env []string

	// NetworkMode is "none": executors must not reach the network.
	NetworkMode string

	// Binds maps the per-container host directory to the executor's socket
	// directory (read-write) and the host site-packages tree (read-only).
	Binds []string

	// MemoryBytes caps container memory. The router transmits this value in a
	// field named cpu_alloc; see container.Allocation for why.
	MemoryBytes int64

	CPUQuota  int64
	PidsLimit int64

	// Runtime selects the OCI runtime. Empty uses the daemon default; "runsc"
	// selects gVisor.
	Runtime string
}

// StartSpec describes how to start an existing container.
type StartSpec struct {
	// CheckpointID is empty for a cold start, or names a checkpoint to restore.
	CheckpointID string
	// CheckpointDir is the directory holding checkpoint images.
	CheckpointDir string
}

// UpdateSpec describes a resource update applied to a running container.
type UpdateSpec struct {
	MemoryBytes int64
}

// Info is the subset of container inspection the worker uses.
type Info struct {
	ID          string
	Name        string
	Image       string
	MemoryBytes int64
	Running     bool
	Paused      bool
}

// Summary is the subset of a container listing the worker uses.
type Summary struct {
	ID    string
	Names []string
	State string
}

// Stats is one resource sample, normalised out of the daemon's wire format.
//
// CPU figures are cumulative counters, not rates. Use CPUPercent to derive a
// utilisation figure from a sample and its predecessor.
type Stats struct {
	Read time.Time

	MemoryUsage int64
	MemoryLimit int64

	CPUTotal     int64 // container CPU time consumed, nanoseconds
	CPUSystem    int64 // host CPU time over the same window, nanoseconds
	PreCPUTotal  int64 // the previous sample's CPUTotal, if the daemon supplied one
	PreCPUSystem int64 // the previous sample's CPUSystem
	OnlineCPUs   int64
}

// CPUPercent returns CPU utilisation for this sample as a percentage of a
// single core times the number of online CPUs, matching `docker stats`.
//
// It returns 0 when the sample carries no usable predecessor, which is the
// case for the first sample of a stream.
func (s Stats) CPUPercent() float64 {
	// An absent predecessor arrives as a zero-valued PreCPUStats. Differencing
	// against it would report cumulative usage since container start as though
	// it were an instantaneous rate, so treat it as no reading at all.
	if s.PreCPUSystem == 0 {
		return 0
	}

	cpuDelta := float64(s.CPUTotal - s.PreCPUTotal)
	systemDelta := float64(s.CPUSystem - s.PreCPUSystem)
	if cpuDelta <= 0 || systemDelta <= 0 {
		return 0
	}
	cpus := float64(s.OnlineCPUs)
	if cpus <= 0 {
		cpus = 1
	}
	return (cpuDelta / systemDelta) * cpus * 100
}

// StatsStream yields samples until io.EOF.
//
// Close releases the underlying connection and unblocks a pending Recv, which
// is how a caller cancels a streaming subscription.
type StatsStream interface {
	Recv() (Stats, error)
	Close() error
}

// Lifecycle mutates containers.
type Lifecycle interface {
	Create(ctx context.Context, spec CreateSpec) (id string, err error)
	Start(ctx context.Context, id string, spec StartSpec) error
	// Stop asks a container to exit, forcing it after timeout. A zero timeout
	// uses the daemon default.
	Stop(ctx context.Context, id string, timeout time.Duration) error
	Remove(ctx context.Context, id string, force bool) error
	Pause(ctx context.Context, id string) error
	Unpause(ctx context.Context, id string) error
	Update(ctx context.Context, id string, spec UpdateSpec) error
}

// Introspector observes containers.
type Introspector interface {
	Inspect(ctx context.Context, id string) (Info, error)
	// List returns containers whose name begins with namePrefix, including
	// stopped ones.
	List(ctx context.Context, namePrefix string) ([]Summary, error)
	// Logs returns demultiplexed stdout and stderr. The caller closes it.
	Logs(ctx context.Context, id string, follow bool) (io.ReadCloser, error)
	// Stats subscribes to resource samples. The caller closes the stream.
	Stats(ctx context.Context, id string, stream bool) (StatsStream, error)
}

// Port is the seam between the worker and any container runtime.
type Port interface {
	Lifecycle
	Introspector

	// Ping verifies the daemon is reachable.
	Ping(ctx context.Context) error
	Close() error
}
