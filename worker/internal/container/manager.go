package container

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path"
	"sync"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/artifact"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

// Sentinel errors returned by a Manager.
var (
	// ErrAcquireFailed indicates no container could be made ready for a request.
	ErrAcquireFailed = errors.New("could not acquire a container")
	// ErrInvalidDeps indicates NewManager was called with a missing dependency.
	ErrInvalidDeps = errors.New("invalid manager dependencies")
)

// Allocation is the compute reserved for an execution.
//
// CPUAlloc is a byte count, not a core count: the router transmits a memory
// budget in registry.proto's cpu_alloc field and the worker maps it onto the
// container's memory limit. The name is preserved for wire compatibility.
type Allocation struct {
	CPUAlloc int64
	GPUAlloc int64
}

// Handle identifies an acquired container.
type Handle struct {
	ID string
	// CheckpointID is the checkpoint the container was restored from, or empty
	// if it started cold — including when a requested restore failed and was
	// downgraded.
	CheckpointID string
	Alloc        Allocation
	// Created reports whether this acquisition created the container, as
	// opposed to resuming an existing one.
	Created bool
}

// Outcome tells Release how an execution finished.
//
// The zero value is OutcomeFailure so that a caller which returns before
// explicitly recording success gets the safe behaviour: the container is
// destroyed rather than returned to the warm pool in an unknown state.
type Outcome int

const (
	// OutcomeFailure destroys the container.
	OutcomeFailure Outcome = iota
	// OutcomeSuccess pauses the container for reuse.
	OutcomeSuccess
)

// String renders an Outcome for logs.
func (o Outcome) String() string {
	if o == OutcomeSuccess {
		return "success"
	}
	return "failure"
}

// AcquireRequest describes the container an execution needs.
type AcquireRequest struct {
	Ref registry.ExecutionRef
	// ContainerID names an existing container to resume. Empty creates one.
	ContainerID string
	// CheckpointID names a checkpoint to restore a new container from.
	CheckpointID string
	Alloc        Allocation
}

// Spec is the fixed part of every container this worker creates.
type Spec struct {
	NamePrefix string
	CPUQuota   int64
	CPUPeriod  int64
	PidsLimit  int64
	// RootReadonly is coupled to the runtime's overlay mode; both must match
	// what a checkpoint was captured under.
	RootReadonly bool
	// CgroupParent is prepended to each sandbox's cgroup path. Empty lets the
	// runtime choose.
	CgroupParent string
	// StopTimeout bounds how long a container may take to exit before it is
	// killed. It must stay well inside the supervisor's own shutdown grace
	// period, since reclamation happens during shutdown.
	StopTimeout time.Duration
	// DirPerm is the mode for per-container host directories. The executor runs
	// as an arbitrary uid, so it must be able to create its socket here.
	DirPerm os.FileMode
}

// SpecFromConfig derives the container spec from worker configuration.
func SpecFromConfig(cfg config.Config) Spec {
	return Spec{
		NamePrefix:   cfg.ContainerNamePrefix,
		CPUQuota:     cfg.CPUQuota,
		CPUPeriod:    cfg.CPUPeriod,
		PidsLimit:    cfg.PidsLimit,
		RootReadonly: cfg.SandboxRootReadonly,
		CgroupParent: cfg.CgroupParent,
		StopTimeout:  cfg.ContainerStopTimeout,
		DirPerm:      0o777,
	}
}

// SandboxExecutorDir is where a container's host directory is mounted inside
// its sandbox. The executor binds its socket there and the worker dials it, so
// the offline pipeline must describe the same mount or a restored sandbox is
// unreachable.
func SandboxExecutorDir() string { return config.ExecutorMountPath }

// Artifacts resolves the rootfs and checkpoint images a sandbox runs against.
// *artifact.Registry satisfies it.
type Artifacts interface {
	// Active is the generation cold starts use.
	Active() artifact.Generation
	// ResolveCheckpoint finds a checkpoint and the generation that owns it.
	ResolveCheckpoint(ref string) (artifact.Checkpoint, artifact.Generation, error)
}

// ManagerDeps collects a Manager's collaborators.
type ManagerDeps struct {
	Runtime   sandbox.Port
	Reporter  registry.Reporter
	Layout    Layout
	Names     NameGenerator
	FS        FS
	Artifacts Artifacts
	Spec      Spec
	Log       *slog.Logger
}

// Manager owns executor container lifecycles.
type Manager struct {
	runtime   sandbox.Port
	reporter  registry.Reporter
	layout    Layout
	names     NameGenerator
	fs        FS
	artifacts Artifacts
	spec      Spec
	log       *slog.Logger

	// live tracks acquired containers so the worker can report its own load.
	//
	// The Manager was otherwise stateless with respect to handles: it created,
	// paused and destroyed containers without remembering any of them, and the
	// runtime is the only durable record. That is still true of correctness —
	// nothing here is consulted to decide anything — but "how much of this
	// worker is spoken for" cannot be answered without it, and asking the
	// runtime on every report would mean an exec per scheduling tick.
	mu   sync.Mutex
	live map[string]Allocation
}

// Load reports how much of this worker is currently committed.
//
// Both numbers count *acquired* containers, not merely existing ones: a paused
// container waiting in the warm pool holds disk and memory pages but is not
// reserved against anyone, and scheduling it as occupied would waste it.
func (m *Manager) Load() (count int32, reservedBytes int64) {
	m.mu.Lock()
	defer m.mu.Unlock()

	for _, alloc := range m.live {
		reservedBytes += alloc.CPUAlloc
	}
	return int32(len(m.live)), reservedBytes
}

func (m *Manager) track(h Handle) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.live[h.ID] = h.Alloc
}

func (m *Manager) untrack(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.live, id)
}

// NewManager validates its dependencies and returns a Manager.
func NewManager(deps ManagerDeps) (*Manager, error) {
	var missing []string
	if deps.Runtime == nil {
		missing = append(missing, "Runtime")
	}
	if deps.Reporter == nil {
		missing = append(missing, "Reporter")
	}
	if deps.Layout == nil {
		missing = append(missing, "Layout")
	}
	if deps.Names == nil {
		missing = append(missing, "Names")
	}
	if deps.FS == nil {
		missing = append(missing, "FS")
	}
	if deps.Artifacts == nil {
		missing = append(missing, "Artifacts")
	}
	if deps.Spec.NamePrefix == "" {
		missing = append(missing, "Spec.NamePrefix")
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("%w: %v", ErrInvalidDeps, missing)
	}

	log := deps.Log
	if log == nil {
		log = slog.Default()
	}
	return &Manager{
		runtime:   deps.Runtime,
		reporter:  deps.Reporter,
		layout:    deps.Layout,
		names:     deps.Names,
		fs:        deps.FS,
		artifacts: deps.Artifacts,
		spec:      deps.Spec,
		log:       log,
		live:      make(map[string]Allocation),
	}, nil
}

// Acquire readies a container for an execution, creating one or resuming the
// named one, and reports it BUSY to the router.
func (m *Manager) Acquire(ctx context.Context, req AcquireRequest) (Handle, error) {
	var (
		handle Handle
		err    error
	)
	if req.ContainerID == "" {
		handle, err = m.create(ctx, req)
	} else {
		handle, err = m.resume(ctx, req)
	}
	if err != nil {
		return Handle{}, fmt.Errorf("%w: %w", ErrAcquireFailed, err)
	}

	m.track(handle)
	m.report(ctx, req.Ref, handle.ID, pb.Status_STATUS_BUSY)
	return handle, nil
}

// create provisions a fresh container and starts it.
//
// The generation is resolved first, because it decides which root filesystem
// the sandbox runs against. A checkpoint can only be restored into the
// filesystem it was captured from, so the rootfs is looked up *through* the
// checkpoint rather than assumed; a cold start falls back to the active
// generation.
func (m *Manager) create(ctx context.Context, req AcquireRequest) (Handle, error) {
	name := m.names.NewName()
	log := m.log.With(logging.KeyContainerID, name, logging.KeyRequestID, req.Ref.RequestID)

	generation, checkpoint, restoreErr := m.resolve(req.CheckpointID)
	if restoreErr != nil {
		// Not fatal: an unresolvable checkpoint downgrades to a cold start on
		// the active generation, exactly as a failed restore does.
		log.Warn("cannot resolve the requested checkpoint; starting cold",
			logging.KeyCheckpoint, req.CheckpointID, logging.KeyError, restoreErr)
	}

	dir := m.layout.ContainerDir(name)
	// A stale directory from a crashed predecessor would leave a dead socket
	// in place, so clear it first. Both operations are checked: the previous
	// implementation discarded the RemoveAll error by overwriting it.
	if err := errors.Join(m.fs.RemoveAll(dir), m.fs.MkdirAll(dir, m.spec.DirPerm)); err != nil {
		return Handle{}, fmt.Errorf("prepare container directory %s: %w", dir, err)
	}

	id, err := m.runtime.Create(ctx, m.createSpec(name, req.Alloc, generation))
	if err != nil {
		// Nothing was created, so the directory is garbage; leaving it behind
		// would slowly fill the worker's disk.
		if rmErr := m.fs.RemoveAll(dir); rmErr != nil {
			log.Warn("could not remove the directory of a container that failed to create",
				logging.KeyError, rmErr)
		}
		return Handle{}, fmt.Errorf("create container: %w", err)
	}
	log.Info("container created", "runtime_id", id)

	checkpointID, err := m.start(ctx, name, checkpoint, log)
	if err != nil {
		return Handle{}, err
	}

	return Handle{ID: name, CheckpointID: checkpointID, Alloc: req.Alloc, Created: true}, nil
}

// resolve maps a checkpoint reference to the generation that owns it.
//
// An empty or unresolvable reference yields the active generation and no
// checkpoint, which is the cold-start case.
func (m *Manager) resolve(checkpointRef string) (artifact.Generation, artifact.Checkpoint, error) {
	if checkpointRef == "" {
		return m.artifacts.Active(), artifact.Checkpoint{}, nil
	}

	checkpoint, generation, err := m.artifacts.ResolveCheckpoint(checkpointRef)
	if err != nil {
		return m.artifacts.Active(), artifact.Checkpoint{}, err
	}
	return generation, checkpoint, nil
}

// start starts a container, downgrading to a cold start if a checkpoint
// restore fails. It returns the checkpoint actually used, which is empty after
// a downgrade so that callers report the truth to the router.
func (m *Manager) start(ctx context.Context, id string, checkpoint artifact.Checkpoint, log *slog.Logger) (string, error) {
	if checkpoint.ID != "" {
		err := m.runtime.Start(ctx, id, sandbox.StartSpec{
			CheckpointID:  checkpoint.ID,
			CheckpointDir: checkpoint.Dir,
		})
		if err == nil {
			log.Info("container restored from checkpoint",
				logging.KeyCheckpoint, checkpoint.ID, "generation", checkpoint.GenerationID)
			return checkpoint.ID, nil
		}
		log.Warn("checkpoint restore failed, falling back to a cold start",
			logging.KeyCheckpoint, checkpoint.ID, logging.KeyError, err)
	}

	if err := m.runtime.Start(ctx, id, sandbox.StartSpec{}); err != nil {
		return "", fmt.Errorf("start container: %w", err)
	}
	log.Info("container started")
	return "", nil
}

// resume unpauses an existing container and re-applies its resource limits.
func (m *Manager) resume(ctx context.Context, req AcquireRequest) (Handle, error) {
	log := m.log.With(logging.KeyContainerID, req.ContainerID, logging.KeyRequestID, req.Ref.RequestID)

	if err := m.runtime.Unpause(ctx, req.ContainerID); err != nil {
		m.report(ctx, req.Ref, req.ContainerID, pb.Status_STATUS_ERROR)
		return Handle{}, fmt.Errorf("unpause container: %w", err)
	}

	if err := m.runtime.Update(ctx, req.ContainerID, sandbox.UpdateSpec{MemoryBytes: req.Alloc.CPUAlloc}); err != nil {
		m.report(ctx, req.Ref, req.ContainerID, pb.Status_STATUS_ERROR)
		return Handle{}, fmt.Errorf("update container resources: %w", err)
	}
	log.Info("container resumed")

	return Handle{
		ID:           req.ContainerID,
		CheckpointID: req.CheckpointID,
		Alloc:        req.Alloc,
	}, nil
}

// Release returns a container to the pool on success, or destroys it otherwise.
//
// Callers pass the zero Outcome when unwinding from an error, so the failure
// path is the default rather than something that has to be remembered.
func (m *Manager) Release(ctx context.Context, ref registry.ExecutionRef, h Handle, outcome Outcome) error {
	if h.ID == "" {
		return nil
	}
	// Untrack before the runtime call, not after: pausing can fail, and a
	// container the caller has finished with is not reserved for anyone
	// regardless of whether the runtime cooperated.
	m.untrack(h.ID)

	if outcome == OutcomeSuccess {
		return m.Pause(ctx, ref, h.ID)
	}
	return m.Destroy(ctx, ref, h.ID)
}

// Pause suspends a container so a later request can reuse it warm.
func (m *Manager) Pause(ctx context.Context, ref registry.ExecutionRef, id string) error {
	if err := m.runtime.Pause(ctx, id); err != nil {
		m.report(ctx, ref, id, pb.Status_STATUS_ERROR)
		return fmt.Errorf("pause container %s: %w", id, err)
	}

	m.log.Info("container paused", logging.KeyContainerID, id)
	m.report(ctx, ref, id, pb.Status_STATUS_READY)
	return nil
}

// Destroy stops and removes a container and deletes its host directory.
//
// The directory is always removed, even when stopping or removing fails, so a
// wedged container cannot leak disk indefinitely.
func (m *Manager) Destroy(ctx context.Context, ref registry.ExecutionRef, id string) error {
	log := m.log.With(logging.KeyContainerID, id)

	defer func() {
		if err := m.fs.RemoveAll(m.layout.ContainerDir(id)); err != nil {
			log.Warn("could not remove container directory", logging.KeyError, err)
		}
	}()

	// A paused container's processes are frozen and cannot act on SIGTERM, so
	// stopping one blocks for the entire stop timeout. Since warm containers
	// are left paused for reuse, shutdown would otherwise stall long enough for
	// the supervisor's own grace period to expire and SIGKILL the worker
	// mid-cleanup. Unpausing first makes the signal deliverable.
	if err := m.runtime.Unpause(ctx, id); err != nil &&
		!errors.Is(err, sandbox.ErrNotFound) && !errors.Is(err, sandbox.ErrConflict) {
		log.Debug("could not unpause before stopping", logging.KeyError, err)
	}

	var errs []error
	if err := m.runtime.Stop(ctx, id, m.spec.StopTimeout); err != nil && !errors.Is(err, sandbox.ErrNotFound) {
		errs = append(errs, fmt.Errorf("stop container %s: %w", id, err))
	}

	// Removal is forced so a container that refused to stop still goes away.
	if err := m.runtime.Remove(ctx, id, true); err != nil && !errors.Is(err, sandbox.ErrNotFound) {
		errs = append(errs, fmt.Errorf("remove container %s: %w", id, err))
	}

	if err := errors.Join(errs...); err != nil {
		m.report(ctx, ref, id, pb.Status_STATUS_ERROR)
		return err
	}

	log.Info("container removed")
	m.report(ctx, ref, id, pb.Status_STATUS_REMOVED)
	return nil
}

// Checkpoint snapshots a warm container so later requests can restore from it.
//
// There is no trigger for this today: execution.proto carries checkpoint_id as
// an input only, so the router has no way to ask for a snapshot. The capability
// exists and is tested; wiring a policy to it needs a protocol change.
func (m *Manager) Checkpoint(ctx context.Context, id, checkpointID, dir string) error {
	if err := m.runtime.Checkpoint(ctx, id, sandbox.CheckpointSpec{
		Dir:          dir,
		LeaveRunning: true,
	}); err != nil {
		return fmt.Errorf("checkpoint container %s: %w", id, err)
	}

	m.log.Info("container checkpointed",
		logging.KeyContainerID, id, logging.KeyCheckpoint, checkpointID, "image", dir)
	return nil
}

// Inspect reports a container's current allocation.
func (m *Manager) Inspect(ctx context.Context, id, checkpointID string) (Handle, error) {
	info, err := m.runtime.Inspect(ctx, id)
	if err != nil {
		return Handle{}, fmt.Errorf("inspect container %s: %w", id, err)
	}

	return Handle{
		ID:           id,
		CheckpointID: checkpointID,
		Alloc:        Allocation{CPUAlloc: info.MemoryBytes},
	}, nil
}

// Logs returns a container's demultiplexed output. The caller closes it.
func (m *Manager) Logs(ctx context.Context, id string) (io.ReadCloser, error) {
	logs, err := m.runtime.Logs(ctx, id, true)
	if err != nil {
		return nil, fmt.Errorf("open logs for container %s: %w", id, err)
	}
	return logs, nil
}

// Stats subscribes to a container's resource samples. The caller closes it.
func (m *Manager) Stats(ctx context.Context, id string) (sandbox.StatsStream, error) {
	stream, err := m.runtime.Stats(ctx, id, true)
	if err != nil {
		return nil, fmt.Errorf("open stats for container %s: %w", id, err)
	}
	return stream, nil
}

// CleanupOrphans destroys every container this worker owns.
//
// It runs at startup as well as shutdown: a process killed without warning
// leaves containers and host directories behind, and startup is the only
// opportunity to reclaim them.
func (m *Manager) CleanupOrphans(ctx context.Context) error {
	summaries, err := m.runtime.List(ctx, m.spec.NamePrefix)
	if err != nil {
		return fmt.Errorf("list orphaned containers: %w", err)
	}
	if len(summaries) == 0 {
		return nil
	}
	m.log.Info("reclaiming orphaned containers", "count", len(summaries))

	var errs []error
	for _, summary := range summaries {
		if err := m.Destroy(ctx, registry.ExecutionRef{}, summary.ID); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// createSpec builds the sandbox specification.
//
// Every field is load-bearing. The executor locates its socket through
// EXECUTOR_DIR, and the /tmp bind is what makes that socket visible to the
// worker. The shape of this spec also has to match the sandbox a checkpoint
// was captured from, so changing it invalidates existing checkpoints; see the
// runsc adapter's Fingerprint.
//
// Note there is no site-packages mount: the generation's root filesystem
// already carries the Python environment, so PYTHONPATH points inside the
// rootfs and the sandbox needs exactly one bind.
func (m *Manager) createSpec(name string, alloc Allocation, generation artifact.Generation) sandbox.CreateSpec {
	env := []string{"EXECUTOR_DIR=" + config.ExecutorMountPath}
	if generation.PythonPath != "" {
		env = append(env, "PYTHONPATH="+generation.PythonPath)
	}

	return sandbox.CreateSpec{
		ID:           name,
		BundleDir:    m.layout.BundleDir(name),
		LogPath:      m.layout.LogPath(name),
		RootfsPath:   generation.RootfsPath(),
		RootReadonly: m.spec.RootReadonly,
		Args:         generation.Argv(),
		Env:          env,
		Cwd:          "/",
		Mounts: []sandbox.Mount{{
			Source:      m.layout.ContainerDir(name),
			Destination: config.ExecutorMountPath,
			Type:        "bind",
			Options:     []string{"rbind", "rw"},
		}},
		MemoryBytes: alloc.CPUAlloc,
		CPUQuota:    m.spec.CPUQuota,
		CPUPeriod:   m.spec.CPUPeriod,
		PidsLimit:   m.spec.PidsLimit,
		CgroupsPath: m.cgroupsPath(name),
	}
}

// cgroupsPath places a sandbox under the configured parent.
func (m *Manager) cgroupsPath(name string) string {
	if m.spec.CgroupParent == "" {
		return ""
	}
	return path.Join(m.spec.CgroupParent, name)
}

// report posts a status to the router. Reporting is best-effort: a router
// outage degrades scheduling but must not fail an execution.
func (m *Manager) report(ctx context.Context, ref registry.ExecutionRef, id string, status pb.Status) {
	if err := m.reporter.ExecutorStatus(ctx, ref, id, status); err != nil {
		m.log.Warn("could not report executor status to the router",
			logging.KeyContainerID, id, "status", status.String(), logging.KeyError, err)
	}
}
