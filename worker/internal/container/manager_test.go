package container_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/docker"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakedocker"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

const sitePackages = "/opt/conda/lib/python3.11/site-packages"

type fixture struct {
	manager   *container.Manager
	docker    *fakedocker.Docker
	registry  *fakeregistry.Recorder
	layout    container.DirLayout
	workerDir string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()

	workerDir := t.TempDir()
	fake := fakedocker.New()
	recorder := fakeregistry.NewRecorder()
	layout := container.NewDirLayout(workerDir)

	manager, err := container.NewManager(container.ManagerDeps{
		Docker:   fake,
		Reporter: recorder,
		Layout:   layout,
		Names:    container.NewUUIDNamer("exec_container-"),
		FS:       container.NewOSFS(),
		Spec: container.Spec{
			Image:            "test-agent",
			NamePrefix:       "exec_container-",
			SitePackagesPath: sitePackages,
			CPUQuota:         50000,
			PidsLimit:        100,
			NetworkMode:      "none",
			DirPerm:          0o777,
		},
		Log: logging.Discard(),
	})
	require.NoError(t, err)

	return &fixture{manager: manager, docker: fake, registry: recorder, layout: layout, workerDir: workerDir}
}

func acquireNew(t *testing.T, f *fixture) container.Handle {
	t.Helper()
	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Ref:   registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"},
		Alloc: container.Allocation{CPUAlloc: 512 << 20},
	})
	require.NoError(t, err)
	return h
}

// The create specification is the contract with the Python executor: it finds
// its socket through EXECUTOR_DIR and its imports through PYTHONPATH, and both
// only resolve because of these bind mounts. Every field is asserted so a
// regression here fails loudly rather than as a mysterious container hang.
func TestAcquire_LocksTheExecutorContract(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	specs := f.docker.CreateSpecs()
	require.Len(t, specs, 1)
	spec := specs[0]

	assert.Equal(t, h.ID, spec.Name)
	assert.Equal(t, "test-agent", spec.Image)
	assert.Equal(t, []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/tmp/site_packages"}, spec.Env)
	assert.Equal(t, "none", spec.NetworkMode, "executors must not reach the network")
	assert.Equal(t, []string{
		filepath.Join(f.workerDir, h.ID) + ":/tmp:rw",
		sitePackages + ":/tmp/site_packages:ro",
	}, spec.Binds)
	assert.Equal(t, int64(512<<20), spec.MemoryBytes)
	assert.Equal(t, int64(50000), spec.CPUQuota)
	assert.Equal(t, int64(100), spec.PidsLimit)
	assert.Empty(t, spec.Runtime, "gVisor stays opt-in until the image ships runsc")
}

func TestAcquire_CreatesTheHostDirectoryTheSocketLivesIn(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	info, err := os.Stat(f.layout.ContainerDir(h.ID))
	require.NoError(t, err)
	assert.True(t, info.IsDir())

	// The worker dials this path; the executor binds the same file at
	// /tmp/executor.sock inside the container.
	assert.Equal(t, filepath.Join(f.workerDir, h.ID, "executor.sock"), f.layout.SocketPath(h.ID))
}

// The previous generator returned a constant, so two concurrent requests
// collided on both the Docker container name and the host directory.
func TestAcquire_GeneratesADistinctNamePerContainer(t *testing.T) {
	f := newFixture(t)

	const n = 100
	seen := make(map[string]bool, n)
	for range n {
		h := acquireNew(t, f)
		require.False(t, seen[h.ID], "duplicate container name %q", h.ID)
		seen[h.ID] = true
		assert.Contains(t, h.ID, "exec_container-")
	}
	assert.Len(t, seen, n)
}

func TestAcquire_IsSafeUnderConcurrency(t *testing.T) {
	f := newFixture(t)

	const n = 20
	var (
		wg  sync.WaitGroup
		mu  sync.Mutex
		ids []string
	)
	for range n {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
				Alloc: container.Allocation{CPUAlloc: 1 << 20},
			})
			assert.NoError(t, err)

			mu.Lock()
			ids = append(ids, h.ID)
			mu.Unlock()
		}()
	}
	wg.Wait()

	unique := make(map[string]bool, n)
	for _, id := range ids {
		unique[id] = true
	}
	assert.Len(t, unique, n, "concurrent acquisitions must not collide")
}

func TestAcquire_ReportsBusyToTheRouter(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	assert.Equal(t, []pb.Status{pb.Status_STATUS_BUSY}, f.registry.StatusesFor(h.ID))

	calls := f.registry.Calls()
	require.NotEmpty(t, calls)
	assert.Equal(t, registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"}, calls[0].Ref)
}

func TestAcquire_RestoresFromACheckpoint(t *testing.T) {
	f := newFixture(t)

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_1",
		Alloc:        container.Allocation{CPUAlloc: 1 << 20},
	})
	require.NoError(t, err)

	assert.Equal(t, "checkpoint_1", h.CheckpointID)
	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.Equal(t, "checkpoint_1", state.StartedFrom)
}

// A failed restore must downgrade to a cold start and report the truth: the
// container is not running the checkpoint the router asked for.
func TestAcquire_DowngradesToAColdStartWhenRestoreFails(t *testing.T) {
	f := newFixture(t)
	f.docker.FailCheckpointStart = true

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_1",
		Alloc:        container.Allocation{CPUAlloc: 1 << 20},
	})
	require.NoError(t, err)

	assert.Empty(t, h.CheckpointID, "a downgraded start must not claim the checkpoint")
	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Started)
	assert.Empty(t, state.StartedFrom)
}

func TestAcquire_CleansUpTheDirectoryWhenCreateFails(t *testing.T) {
	f := newFixture(t)
	f.docker.FailOn("Create", errors.New("no such image"))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: container.Allocation{CPUAlloc: 1 << 20},
	})
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrAcquireFailed)

	entries, err := os.ReadDir(f.workerDir)
	require.NoError(t, err)
	assert.Empty(t, entries, "a failed create must not leave a directory behind")
}

func TestAcquire_ResumesAnExistingContainer(t *testing.T) {
	f := newFixture(t)
	first := acquireNew(t, f)
	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, first, container.OutcomeSuccess))

	second, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		ContainerID: first.ID,
		Alloc:       container.Allocation{CPUAlloc: 256 << 20},
	})
	require.NoError(t, err)

	assert.Equal(t, first.ID, second.ID)
	assert.False(t, second.Created)
	assert.Len(t, f.docker.CreateSpecs(), 1, "resuming must not create a second container")

	state, ok := f.docker.Get(first.ID)
	require.True(t, ok)
	assert.False(t, state.Paused)
	assert.Equal(t, int64(256<<20), state.Memory, "the new allocation must be applied")
}

func TestAcquire_ReportsErrorWhenResumeFails(t *testing.T) {
	f := newFixture(t)
	first := acquireNew(t, f)
	f.docker.FailOn("Unpause", docker.ErrConflict)

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{ContainerID: first.ID})
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrAcquireFailed)
	assert.Contains(t, f.registry.StatusesFor(first.ID), pb.Status_STATUS_ERROR)
}

func TestRelease_SuccessPausesForReuse(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess))

	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Paused)
	assert.False(t, state.Removed)

	assert.Equal(t, []pb.Status{pb.Status_STATUS_BUSY, pb.Status_STATUS_READY}, f.registry.StatusesFor(h.ID))
	assert.DirExists(t, f.layout.ContainerDir(h.ID), "a reusable container keeps its socket directory")
}

func TestRelease_FailureDestroysTheContainerAndItsDirectory(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, container.OutcomeFailure))

	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)

	assert.Equal(t, []pb.Status{pb.Status_STATUS_BUSY, pb.Status_STATUS_REMOVED}, f.registry.StatusesFor(h.ID))
	assert.NoDirExists(t, f.layout.ContainerDir(h.ID))
}

// The zero Outcome is what an unwinding caller passes, so it must be the
// destructive one: a container abandoned mid-execution is in an unknown state
// and must not go back into the warm pool.
func TestRelease_ZeroOutcomeDestroys(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	var outcome container.Outcome // deliberately not assigned
	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, outcome))

	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
}

func TestDestroy_RemovesTheDirectoryEvenWhenDockerFails(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)
	f.docker.FailOn("Stop", errors.New("daemon is wedged"))
	f.docker.FailOn("Remove", errors.New("daemon is wedged"))

	err := f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID)
	require.Error(t, err)

	assert.NoDirExists(t, f.layout.ContainerDir(h.ID),
		"a wedged container must not leak its directory")
	assert.Contains(t, f.registry.StatusesFor(h.ID), pb.Status_STATUS_ERROR)
}

func TestDestroy_TreatsAnAlreadyGoneContainerAsSuccess(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	// A second destroy races with nothing but must still be harmless.
	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))
}

func TestCleanupOrphans_ReclaimsContainersFromAPreviousProcess(t *testing.T) {
	f := newFixture(t)

	orphans := []string{"exec_container-orphan-a", "exec_container-orphan-b"}
	for _, id := range orphans {
		f.docker.Seed(id)
		require.NoError(t, os.MkdirAll(f.layout.ContainerDir(id), 0o777))
	}
	// An unrelated container must survive.
	f.docker.Seed("some-other-service")

	require.NoError(t, f.manager.CleanupOrphans(context.Background()))

	for _, id := range orphans {
		state, ok := f.docker.Get(id)
		require.True(t, ok)
		assert.True(t, state.Removed, "%s should have been reclaimed", id)
		assert.NoDirExists(t, f.layout.ContainerDir(id))
	}

	other, ok := f.docker.Get("some-other-service")
	require.True(t, ok)
	assert.False(t, other.Removed, "containers this worker does not own must be left alone")
}

func TestCleanupOrphans_NoOpWhenThereAreNone(t *testing.T) {
	f := newFixture(t)
	require.NoError(t, f.manager.CleanupOrphans(context.Background()))
}

func TestCleanupOrphans_SurfacesListFailures(t *testing.T) {
	f := newFixture(t)
	f.docker.FailOn("List", docker.ErrDaemonUnavailable)

	err := f.manager.CleanupOrphans(context.Background())
	require.Error(t, err)
	assert.ErrorIs(t, err, docker.ErrDaemonUnavailable)
}

func TestInspect_ReportsTheAppliedAllocation(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	inspected, err := f.manager.Inspect(context.Background(), h.ID, "checkpoint_1")
	require.NoError(t, err)

	assert.Equal(t, h.ID, inspected.ID)
	assert.Equal(t, "checkpoint_1", inspected.CheckpointID)
	assert.Equal(t, int64(512<<20), inspected.Alloc.CPUAlloc)
}

func TestNewManager_RejectsMissingDependencies(t *testing.T) {
	_, err := container.NewManager(container.ManagerDeps{})
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrInvalidDeps)

	for _, want := range []string{"Docker", "Reporter", "Layout", "Names", "FS", "Spec.Image"} {
		assert.Contains(t, err.Error(), want)
	}
}

// A router outage degrades scheduling but must never fail an execution.
func TestManager_TreatsRouterFailuresAsNonFatal(t *testing.T) {
	f := newFixture(t)
	f.registry.FailOn("ExecutorStatus", errors.New("router is down"))

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: container.Allocation{CPUAlloc: 1 << 20},
	})
	require.NoError(t, err)
	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess))
}

// A paused container's processes are frozen and cannot act on SIGTERM, so
// stopping one blocks for the whole stop timeout. Warm containers are left
// paused, so shutdown must unpause before stopping or it stalls long enough
// for the supervisor to SIGKILL the worker mid-cleanup.
func TestDestroy_UnpausesBeforeStoppingAPausedContainer(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	// Leave it paused the way a completed execution does.
	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess))
	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	require.True(t, state.Paused)

	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	state, ok = f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
	assert.False(t, state.Paused)
}

// Unpausing a container that is not paused is a conflict, and must not be
// treated as a failure of the destroy.
func TestDestroy_TolerantOfAnUnpausableContainer(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)
	f.docker.FailOn("Unpause", docker.ErrConflict)

	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	state, ok := f.docker.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
}

// The stop timeout must stay well inside a supervisor's shutdown grace period.
func TestSpecFromConfig_UsesAShortContainerStopTimeout(t *testing.T) {
	cfg, err := config.Load(func(k string) string {
		return map[string]string{
			"SERVICE_NAME": "worker", "PORT": "50052", "WORKER_DIR": "/shared",
			"ROUTER_URI": "router:50051", "SITEPACKAGES_TXT_PATH": "/sp.txt",
		}[k]
	}, func(string) ([]byte, error) { return []byte("/site-packages"), nil })
	require.NoError(t, err)

	spec := container.SpecFromConfig(cfg)
	assert.Positive(t, spec.StopTimeout)
	assert.LessOrEqual(t, spec.StopTimeout, 5*time.Second,
		"reclamation runs during shutdown, inside the supervisor's grace period")
}
