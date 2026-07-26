package container

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/docker"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
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
	Image            string
	NamePrefix       string
	SitePackagesPath string
	CPUQuota         int64
	PidsLimit        int64
	Runtime          string
	NetworkMode      string
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
		Image:            cfg.ExecutorImage,
		NamePrefix:       cfg.ContainerNamePrefix,
		SitePackagesPath: cfg.SitePackagesPath,
		CPUQuota:         cfg.CPUQuota,
		PidsLimit:        cfg.PidsLimit,
		Runtime:          cfg.ContainerRuntime,
		NetworkMode:      cfg.NetworkMode,
		StopTimeout:      cfg.ContainerStopTimeout,
		DirPerm:          0o777,
	}
}

// ManagerDeps collects a Manager's collaborators.
type ManagerDeps struct {
	Docker   docker.Port
	Reporter registry.Reporter
	Layout   Layout
	Names    NameGenerator
	FS       FS
	Spec     Spec
	Log      *slog.Logger
}

// Manager owns executor container lifecycles.
type Manager struct {
	docker   docker.Port
	reporter registry.Reporter
	layout   Layout
	names    NameGenerator
	fs       FS
	spec     Spec
	log      *slog.Logger
}

// NewManager validates its dependencies and returns a Manager.
func NewManager(deps ManagerDeps) (*Manager, error) {
	var missing []string
	if deps.Docker == nil {
		missing = append(missing, "Docker")
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
	if deps.Spec.Image == "" {
		missing = append(missing, "Spec.Image")
	}
	if deps.Spec.NamePrefix == "" {
		missing = append(missing, "Spec.NamePrefix")
	}
	if deps.Spec.SitePackagesPath == "" {
		missing = append(missing, "Spec.SitePackagesPath")
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("%w: %v", ErrInvalidDeps, missing)
	}

	log := deps.Log
	if log == nil {
		log = slog.Default()
	}
	return &Manager{
		docker:   deps.Docker,
		reporter: deps.Reporter,
		layout:   deps.Layout,
		names:    deps.Names,
		fs:       deps.FS,
		spec:     deps.Spec,
		log:      log,
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

	m.report(ctx, req.Ref, handle.ID, pb.Status_STATUS_BUSY)
	return handle, nil
}

// create provisions a fresh container and starts it.
func (m *Manager) create(ctx context.Context, req AcquireRequest) (Handle, error) {
	name := m.names.NewName()
	log := m.log.With(logging.KeyContainerID, name, logging.KeyRequestID, req.Ref.RequestID)

	dir := m.layout.ContainerDir(name)
	// A stale directory from a crashed predecessor would leave a dead socket
	// in place, so clear it first. Both operations are checked: the previous
	// implementation discarded the RemoveAll error by overwriting it.
	if err := errors.Join(m.fs.RemoveAll(dir), m.fs.MkdirAll(dir, m.spec.DirPerm)); err != nil {
		return Handle{}, fmt.Errorf("prepare container directory %s: %w", dir, err)
	}

	id, err := m.docker.Create(ctx, m.createSpec(name, req.Alloc))
	if err != nil {
		// Nothing was created, so the directory is garbage; leaving it behind
		// would slowly fill the worker's disk.
		if rmErr := m.fs.RemoveAll(dir); rmErr != nil {
			log.Warn("could not remove the directory of a container that failed to create",
				logging.KeyError, rmErr)
		}
		return Handle{}, fmt.Errorf("create container: %w", err)
	}
	log.Info("container created", "docker_id", id)

	checkpointID, err := m.start(ctx, name, req.CheckpointID, log)
	if err != nil {
		return Handle{}, err
	}

	return Handle{ID: name, CheckpointID: checkpointID, Alloc: req.Alloc, Created: true}, nil
}

// start starts a container, downgrading to a cold start if a checkpoint
// restore fails. It returns the checkpoint actually used, which is empty after
// a downgrade so that callers report the truth to the router.
func (m *Manager) start(ctx context.Context, id, checkpointID string, log *slog.Logger) (string, error) {
	if checkpointID != "" {
		err := m.docker.Start(ctx, id, docker.StartSpec{
			CheckpointID:  checkpointID,
			CheckpointDir: m.layout.CheckpointDir(),
		})
		if err == nil {
			log.Info("container restored from checkpoint", logging.KeyCheckpoint, checkpointID)
			return checkpointID, nil
		}
		log.Warn("checkpoint restore failed, falling back to a cold start",
			logging.KeyCheckpoint, checkpointID, logging.KeyError, err)
	}

	if err := m.docker.Start(ctx, id, docker.StartSpec{}); err != nil {
		return "", fmt.Errorf("start container: %w", err)
	}
	log.Info("container started")
	return "", nil
}

// resume unpauses an existing container and re-applies its resource limits.
func (m *Manager) resume(ctx context.Context, req AcquireRequest) (Handle, error) {
	log := m.log.With(logging.KeyContainerID, req.ContainerID, logging.KeyRequestID, req.Ref.RequestID)

	if err := m.docker.Unpause(ctx, req.ContainerID); err != nil {
		m.report(ctx, req.Ref, req.ContainerID, pb.Status_STATUS_ERROR)
		return Handle{}, fmt.Errorf("unpause container: %w", err)
	}

	if err := m.docker.Update(ctx, req.ContainerID, docker.UpdateSpec{MemoryBytes: req.Alloc.CPUAlloc}); err != nil {
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
	if outcome == OutcomeSuccess {
		return m.Pause(ctx, ref, h.ID)
	}
	return m.Destroy(ctx, ref, h.ID)
}

// Pause suspends a container so a later request can reuse it warm.
func (m *Manager) Pause(ctx context.Context, ref registry.ExecutionRef, id string) error {
	if err := m.docker.Pause(ctx, id); err != nil {
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
	if err := m.docker.Unpause(ctx, id); err != nil &&
		!errors.Is(err, docker.ErrNotFound) && !errors.Is(err, docker.ErrConflict) {
		log.Debug("could not unpause before stopping", logging.KeyError, err)
	}

	var errs []error
	if err := m.docker.Stop(ctx, id, m.spec.StopTimeout); err != nil && !errors.Is(err, docker.ErrNotFound) {
		errs = append(errs, fmt.Errorf("stop container %s: %w", id, err))
	}

	// Removal is forced so a container that refused to stop still goes away.
	if err := m.docker.Remove(ctx, id, true); err != nil && !errors.Is(err, docker.ErrNotFound) {
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

// Inspect reports a container's current allocation.
func (m *Manager) Inspect(ctx context.Context, id, checkpointID string) (Handle, error) {
	info, err := m.docker.Inspect(ctx, id)
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
	logs, err := m.docker.Logs(ctx, id, true)
	if err != nil {
		return nil, fmt.Errorf("open logs for container %s: %w", id, err)
	}
	return logs, nil
}

// Stats subscribes to a container's resource samples. The caller closes it.
func (m *Manager) Stats(ctx context.Context, id string) (docker.StatsStream, error) {
	stream, err := m.docker.Stats(ctx, id, true)
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
	summaries, err := m.docker.List(ctx, m.spec.NamePrefix)
	if err != nil {
		return fmt.Errorf("list orphaned containers: %w", err)
	}
	if len(summaries) == 0 {
		return nil
	}
	m.log.Info("reclaiming orphaned containers", "count", len(summaries))

	var errs []error
	for _, summary := range summaries {
		// Destroy by name rather than by Docker id, so the host directory —
		// which is named after the container — is removed along with it.
		id := summary.ID
		if len(summary.Names) > 0 {
			id = summary.Names[0]
		}
		if err := m.Destroy(ctx, registry.ExecutionRef{}, id); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// createSpec builds the container specification.
//
// Every field here is load-bearing. The executor locates its socket through
// EXECUTOR_DIR and its imports through PYTHONPATH, and both only resolve
// because of the corresponding bind mounts.
func (m *Manager) createSpec(name string, alloc Allocation) docker.CreateSpec {
	return docker.CreateSpec{
		Name:  name,
		Image: m.spec.Image,
		Env: []string{
			"EXECUTOR_DIR=" + config.ExecutorMountPath,
			"PYTHONPATH=" + config.SitePackagesMountPath,
		},
		NetworkMode: m.spec.NetworkMode,
		Binds: []string{
			fmt.Sprintf("%s:%s:rw", m.layout.ContainerDir(name), config.ExecutorMountPath),
			fmt.Sprintf("%s:%s:ro", m.spec.SitePackagesPath, config.SitePackagesMountPath),
		},
		MemoryBytes: alloc.CPUAlloc,
		CPUQuota:    m.spec.CPUQuota,
		PidsLimit:   m.spec.PidsLimit,
		Runtime:     m.spec.Runtime,
	}
}

// report posts a status to the router. Reporting is best-effort: a router
// outage degrades scheduling but must not fail an execution.
func (m *Manager) report(ctx context.Context, ref registry.ExecutionRef, id string, status pb.Status) {
	if err := m.reporter.ExecutorStatus(ctx, ref, id, status); err != nil {
		m.log.Warn("could not report executor status to the router",
			logging.KeyContainerID, id, "status", status.String(), logging.KeyError, err)
	}
}
