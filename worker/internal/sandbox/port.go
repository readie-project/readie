// Package sandbox is the worker's sole boundary to a sandbox runtime.
//
// Port is a narrow, runtime-agnostic interface expressed in this package's own
// DTOs; every other package depends on it and only on it, which is what makes
// container orchestration testable without a real runtime. internal/runsc
// implements it by driving the runsc(1) CLI and is the only package that knows
// gVisor exists.
package sandbox

import (
	"context"
	"io"
	"time"
)

// Mount is one entry of a sandbox's mount table.
//
// Destination, Type and Options are part of the checkpoint compatibility
// contract: a checkpoint only restores into a sandbox whose ordered mount list
// matches the one it was captured with. Source may differ, because it is
// resolved fresh from the bundle at restore time — which is what lets a
// checkpoint move between workers.
type Mount struct {
	Source      string
	Destination string
	Type        string
	Options     []string
}

// CreateSpec describes a sandbox to materialise.
//
// The field values form a contract with the Python executor: it discovers its
// socket directory through Env, and the bind mount for that directory is what
// makes the socket visible to the worker. See executor/ before
// changing any of them.
type CreateSpec struct {
	// ID names the sandbox. It is the runtime's container id, the per-sandbox
	// directory name, and the identifier in every downstream call and log line.
	ID string

	// BundleDir holds the generated config.json and per-sandbox runtime state.
	// It is not visible inside the sandbox.
	BundleDir string

	// RootfsPath is the host directory used as the sandbox root. It belongs to
	// an artifact generation and is shared read-only by every sandbox using
	// that generation; per-sandbox writes go to the runtime's overlay.
	RootfsPath string
	// RootReadonly is coupled to the runtime's overlay mode and must match
	// between a checkpoint and the sandbox it is restored into.
	RootReadonly bool

	Args []string
	// Env must carry EXECUTOR_DIR; the executor exits non-zero without it.
	Env []string
	Cwd string

	// Mounts is ordered, and parents must precede children.
	Mounts []Mount

	// MemoryBytes caps sandbox memory. The router transmits this value in a
	// field named cpu_alloc; see container.Allocation for why.
	MemoryBytes int64
	CPUQuota    int64
	CPUPeriod   int64
	PidsLimit   int64

	// GPU requests NVIDIA passthrough. It makes BuildSpec add the
	// NVIDIA_VISIBLE_DEVICES env nvproxy-docker reads, and must match the
	// sandbox a checkpoint was captured under (it is in the fingerprint).
	GPU bool

	// CgroupsPath is the cgroup to place the sandbox in. Empty lets the
	// runtime choose.
	CgroupsPath string

	// LogPath receives the sandbox's merged stdout and stderr. The runtime has
	// no log API, so this file is the only source Logs can read.
	LogPath string
}

// StartSpec describes how to bring a created sandbox up.
type StartSpec struct {
	// CheckpointID is empty for a cold start, or names a checkpoint to restore.
	CheckpointID string
	// CheckpointDir is the image directory for that checkpoint.
	CheckpointDir string
}

// CheckpointSpec describes a snapshot to take of a live sandbox.
type CheckpointSpec struct {
	// Dir is the image directory to write. It is created if absent.
	Dir string
	// LeaveRunning keeps the sandbox alive after the snapshot.
	LeaveRunning bool
}

// UpdateSpec describes a resource change applied to a live sandbox.
type UpdateSpec struct {
	MemoryBytes int64
}

// Info is the subset of sandbox state the worker uses.
type Info struct {
	ID          string
	PID         int
	MemoryBytes int64
	Running     bool
	Paused      bool
}

// Summary is the subset of a sandbox listing the worker uses.
type Summary struct {
	ID    string
	State string
}

// Stats is one resource sample.
//
// CPUTotal is a cumulative counter, not a rate. Elapsed is the wall time since
// the previous sample and is zero for the first sample of a stream.
type Stats struct {
	Read time.Time

	MemoryUsage int64
	MemoryLimit int64

	CPUTotal    int64 // cumulative sandbox CPU time, nanoseconds
	PreCPUTotal int64 // the previous sample's CPUTotal
	Elapsed     time.Duration

	OnlineCPUs int64
	Pids       int64
}

// CPUPercent returns CPU utilisation for this sample as a percentage of one
// core, so a fully saturated core reads 100 and two saturated cores read 200.
//
// It returns 0 when the sample has no usable predecessor, which is the case
// for the first sample of a stream. Unlike a container daemon, gVisor exposes
// no host-wide CPU counter, so the denominator is wall-clock time.
func (s Stats) CPUPercent() float64 {
	if s.Elapsed <= 0 {
		return 0
	}
	delta := float64(s.CPUTotal - s.PreCPUTotal)
	if delta <= 0 {
		return 0
	}
	return delta / float64(s.Elapsed.Nanoseconds()) * 100
}

// StatsStream yields samples until io.EOF.
//
// Close releases the underlying resources and unblocks a pending Recv, which
// is how a caller cancels a subscription.
type StatsStream interface {
	Recv() (Stats, error)
	Close() error
}

// Lifecycle mutates sandboxes.
type Lifecycle interface {
	// Create materialises a sandbox's bundle but does not launch it.
	//
	// The split matters: restoring a checkpoint both creates and starts in one
	// runtime call and conflicts with an already-created sandbox, so launching
	// belongs entirely to Start.
	Create(ctx context.Context, spec CreateSpec) (id string, err error)

	// Start launches a created sandbox, restoring it from a checkpoint when
	// StartSpec names one.
	//
	// A failed restore must leave nothing behind, so the caller may retry
	// immediately with a cold start.
	Start(ctx context.Context, id string, spec StartSpec) error

	// Stop asks a sandbox to exit, forcing it after timeout.
	Stop(ctx context.Context, id string, timeout time.Duration) error
	Remove(ctx context.Context, id string, force bool) error
	Pause(ctx context.Context, id string) error
	Unpause(ctx context.Context, id string) error
	Update(ctx context.Context, id string, spec UpdateSpec) error

	// Checkpoint snapshots a sandbox into spec.Dir.
	Checkpoint(ctx context.Context, id string, spec CheckpointSpec) error
}

// Introspector observes sandboxes.
type Introspector interface {
	Inspect(ctx context.Context, id string) (Info, error)
	// List returns sandboxes whose id begins with idPrefix, including stopped
	// ones and ones whose runtime state was lost but whose bundle survives.
	List(ctx context.Context, idPrefix string) ([]Summary, error)
	// Logs returns the sandbox's merged output. The caller closes it.
	Logs(ctx context.Context, id string, follow bool) (io.ReadCloser, error)
	// Stats subscribes to resource samples. The caller closes the stream.
	Stats(ctx context.Context, id string, stream bool) (StatsStream, error)
}

// Port is the seam between the worker and any sandbox runtime.
type Port interface {
	Lifecycle
	Introspector

	// Probe verifies the runtime is usable.
	Probe(ctx context.Context) error
	Close() error
}
