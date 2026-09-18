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

	"github.com/illinoisdata/readie/worker/internal/artifact"
	"github.com/illinoisdata/readie/worker/internal/clock"
	"github.com/illinoisdata/readie/worker/internal/config"
	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
	"github.com/illinoisdata/readie/worker/internal/registry"
	"github.com/illinoisdata/readie/worker/internal/runsc"
	"github.com/illinoisdata/readie/worker/internal/sandbox"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakenetwork"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakeregistry"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakesandbox"
	pb "github.com/illinoisdata/readie/worker/proto"
)

const (
	pythonPath = "/lib/python3.12/dist-packages"
)

var executorArgv = []string{"python", "-u", "-m", "readie_executor"}

// memAlloc is an allocation carrying a single memory budget of n bytes.
func memAlloc(n int64) container.Allocation {
	return container.Allocation{Budgets: []container.Budget{{Kind: container.KindMemory, Alloc: n}}}
}

type fixture struct {
	manager      *container.Manager
	runtime      *fakesandbox.Sandbox
	registry     *fakeregistry.Recorder
	layout       container.DirLayout
	artifacts    container.Artifacts
	workerDir    string
	artifactRoot string
	clock        *clock.Fake
	network      *fakenetwork.Provisioner
}

// buildArtifacts lays out a rootfs, a manifest and the given checkpoints, the
// way the worker image bakes them.
func buildArtifacts(t *testing.T, checkpoints ...string) (string, *artifact.Registry) {
	t.Helper()

	root := t.TempDir()
	require.NoError(t, os.MkdirAll(filepath.Join(root, artifact.RootfsDirName), 0o755))
	require.NoError(t, artifact.WriteManifest(root, artifact.Manifest{
		RunscVersion:     "runsc version test",
		SpecFingerprint:  "sha256:spec",
		ExecutorArgv:     executorArgv,
		ExecutorProtocol: executor.ProtocolVersion,
		PythonPath:       pythonPath,
		CreatedAt:        time.Now().UTC(),
	}))

	for _, id := range checkpoints {
		cdir := filepath.Join(root, artifact.CheckpointsDirName, id)
		require.NoError(t, os.MkdirAll(cdir, 0o755))
		require.NoError(t, os.WriteFile(filepath.Join(cdir, "checkpoint.img"), []byte("img"), 0o600))
		require.NoError(t, artifact.WriteCheckpointMeta(cdir, artifact.Checkpoint{
			ID: id, Producer: "pipeline",
		}))
	}

	registry, err := artifact.Load(artifact.Options{Root: root, Log: logging.Discard()})
	require.NoError(t, err)
	return root, registry
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	_, artifacts := buildArtifacts(t, "checkpoint_1")
	return newFixtureWith(t, artifacts)
}

// newFixtureWith builds a manager over a caller-supplied artifact registry, so
// a test can exercise a worker that holds no root filesystem.
func newFixtureWith(t *testing.T, artifacts container.Artifacts) *fixture {
	t.Helper()
	return newFixtureWithSpec(t, artifacts, container.Spec{
		NamePrefix:   "exec_container-",
		CPUQuota:     50000,
		CPUPeriod:    100000,
		PidsLimit:    100,
		CgroupParent: "/readie",
		DirPerm:      0o777,
	})
}

// newFixtureWithSpec is newFixtureWith with a caller-supplied Spec, so tests
// exercising Spec-driven behaviour (such as PauseTTL) can override just that.
func newFixtureWithSpec(t *testing.T, artifacts container.Artifacts, spec container.Spec) *fixture {
	t.Helper()
	return newFixtureWithNetwork(t, artifacts, spec, nil)
}

// newFixtureWithNetwork is newFixtureWithSpec with a caller-supplied
// network.Provisioner, so tests can exercise the sandbox-network wiring
// without touching every other fixture-building test.
func newFixtureWithNetwork(
	t *testing.T, artifacts container.Artifacts, spec container.Spec, net *fakenetwork.Provisioner,
) *fixture {
	t.Helper()

	workerDir := t.TempDir()
	fake := fakesandbox.New()
	recorder := fakeregistry.NewRecorder()
	layout := container.NewDirLayout(workerDir)
	fakeClock := clock.NewFake(time.Now())
	// Only a real registry has a root on disk; the empty-artifact tests do not
	// need one.
	artifactRoot := ""
	if reg, ok := artifacts.(*artifact.Registry); ok {
		artifactRoot = reg.Root()
	}

	deps := container.ManagerDeps{
		Runtime:   fake,
		Reporter:  recorder,
		Layout:    layout,
		Names:     container.NewUUIDNamer("exec_container-"),
		FS:        container.NewOSFS(),
		Artifacts: artifacts,
		Spec:      spec,
		Log:       logging.Discard(),
		Clock:     fakeClock,
	}
	if net != nil {
		deps.Network = net
	}

	manager, err := container.NewManager(deps)
	require.NoError(t, err)

	return &fixture{
		manager: manager, runtime: fake, registry: recorder, layout: layout,
		artifacts: artifacts, workerDir: workerDir, artifactRoot: artifactRoot,
		clock: fakeClock, network: net,
	}
}

func acquireNew(t *testing.T, f *fixture) container.Handle {
	t.Helper()
	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Ref:   registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"},
		Alloc: memAlloc(512 << 20),
	})
	require.NoError(t, err)
	return h
}

// The create specification is the contract with the Python executor: it finds
// its socket through EXECUTOR_DIR, and the /tmp bind is what makes that socket
// visible to the worker. Every field is asserted so a regression here fails
// loudly rather than as a mysterious dial timeout.
func TestAcquire_LocksTheExecutorContract(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	specs := f.runtime.CreateSpecs()
	require.Len(t, specs, 1)
	spec := specs[0]

	assert.Equal(t, h.ID, spec.ID)
	assert.Equal(t, executorArgv, spec.Args, "the manifest records argv; the worker replays it")
	assert.Equal(t, []string{"EXECUTOR_DIR=/tmp", "EXECUTOR_MODE=sandbox", "PYTHONPATH=" + pythonPath}, spec.Env)
	assert.Equal(t, activeRootfs(t, f), spec.RootfsPath)
	assert.Equal(t, f.layout.BundleDir(h.ID), spec.BundleDir)
	assert.Equal(t, f.layout.LogPath(h.ID), spec.LogPath)

	// Exactly one bind: the rootfs already carries the Python environment, so
	// there is no site-packages mount to keep in sync with a checkpoint.
	require.Len(t, spec.Mounts, 1)
	assert.Equal(t, sandbox.Mount{
		Source:      filepath.Join(f.workerDir, h.ID),
		Destination: "/tmp",
		Type:        "bind",
		Options:     []string{"rbind", "rw"},
	}, spec.Mounts[0])

	assert.Equal(t, int64(512<<20), spec.MemoryBytes)
	assert.Equal(t, int64(50000), spec.CPUQuota)
	assert.Equal(t, int64(100000), spec.CPUPeriod)
	assert.Equal(t, int64(100), spec.PidsLimit)
	assert.Equal(t, "/readie/"+h.ID, spec.CgroupsPath)
}

// The bundle carries the sandbox's OCI spec and must not be reachable from
// inside the sandbox, which can see everything under ContainerDir.
func TestAcquire_KeepsTheBundleOutsideTheSandboxsView(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	assert.NotContains(t, f.layout.BundleDir(h.ID), f.layout.ContainerDir(h.ID),
		"a sandbox has no business reading its own runtime spec")
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
// collided on both the runtime's container id and the host directory.
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
				Alloc: memAlloc(1 << 20),
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
		Alloc:        memAlloc(1 << 20),
	})
	require.NoError(t, err)

	assert.Equal(t, "checkpoint_1", h.CheckpointID)
	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.Equal(t, "checkpoint_1", state.StartedFrom)
}

// A checkpoint only restores into the filesystem it was captured from, so the
// rootfs must be looked up through the checkpoint rather than assumed.
func TestAcquire_RestoreUsesTheCheckpointsOwnGeneration(t *testing.T) {
	f := newFixture(t)

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_1",
		Alloc:        memAlloc(1 << 20),
	})
	require.NoError(t, err)

	checkpoint, err := f.artifacts.ResolveCheckpoint("checkpoint_1")
	require.NoError(t, err)

	// There is one rootfs, so a restore and a cold start run against the same
	// tree - which is what makes the pairing correct by construction.
	specs := f.runtime.CreateSpecs()
	require.Len(t, specs, 1)
	assert.Equal(t, activeRootfs(t, f), specs[0].RootfsPath)
	assert.Equal(t, filepath.Join(f.artifactRoot, artifact.CheckpointsDirName, "checkpoint_1"),
		checkpoint.Dir)
}

// An unresolvable checkpoint is a cold start, not a failure: the request can
// still be served, just without the warm image.
func TestAcquire_UnknownCheckpointFallsBackToAColdStart(t *testing.T) {
	f := newFixture(t)

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_does_not_exist",
		Alloc:        memAlloc(1 << 20),
	})
	require.NoError(t, err)

	assert.Empty(t, h.CheckpointID, "a cold start must not claim a checkpoint")
	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Started)
	assert.Empty(t, state.StartedFrom)
	assert.Equal(t, activeRootfs(t, f), f.runtime.CreateSpecs()[0].RootfsPath)
}

// A failed restore must downgrade to a cold start and report the truth: the
// container is not running the checkpoint the router asked for.
func TestAcquire_DowngradesToAColdStartWhenRestoreFails(t *testing.T) {
	f := newFixture(t)
	f.runtime.FailRestore = true

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_1",
		Alloc:        memAlloc(1 << 20),
	})
	require.NoError(t, err)

	assert.Empty(t, h.CheckpointID, "a downgraded start must not claim the checkpoint")
	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Started)
	assert.Empty(t, state.StartedFrom)
}

func TestAcquire_CleansUpTheDirectoryWhenCreateFails(t *testing.T) {
	f := newFixture(t)
	f.runtime.FailOn("Create", errors.New("no such image"))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(1 << 20),
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
	// A real session, or the release below would destroy rather than pause it.
	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{SessionID: "sess-1"}, first, container.OutcomeSuccess))

	second, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		ContainerID: first.ID,
		Alloc:       memAlloc(256 << 20),
	})
	require.NoError(t, err)

	assert.Equal(t, first.ID, second.ID)
	assert.False(t, second.Created)
	assert.Len(t, f.runtime.CreateSpecs(), 1, "resuming must not create a second container")

	state, ok := f.runtime.Get(first.ID)
	require.True(t, ok)
	assert.False(t, state.Paused)
	assert.Equal(t, int64(256<<20), state.Memory, "the new allocation must be applied")
}

func TestAcquire_ReportsErrorWhenResumeFails(t *testing.T) {
	f := newFixture(t)
	first := acquireNew(t, f)
	f.runtime.FailOn("Unpause", sandbox.ErrConflict)

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{ContainerID: first.ID})
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrAcquireFailed)
	assert.Contains(t, f.registry.StatusesFor(first.ID), pb.Status_STATUS_ERROR)
}

func TestRelease_SuccessPausesForReuse(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{SessionID: "sess-1"}, h, container.OutcomeSuccess))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Paused)
	assert.False(t, state.Removed)

	assert.Equal(t, []pb.Status{pb.Status_STATUS_BUSY, pb.Status_STATUS_READY}, f.registry.StatusesFor(h.ID))
	assert.DirExists(t, f.layout.ContainerDir(h.ID), "a reusable container keeps its socket directory")
}

// A container acquired for a request with no session is never paused, even
// on success: with no session, nothing can ever resume it by ID, so pausing
// it would just hold its memory until SANDBOX_PAUSE_TTL for no one.
func TestRelease_SuccessWithNoSessionDestroysInstead(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.False(t, state.Paused)
	assert.True(t, state.Removed)
}

func TestRelease_FailureDestroysTheContainerAndItsDirectory(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Release(context.Background(), registry.ExecutionRef{}, h, container.OutcomeFailure))

	state, ok := f.runtime.Get(h.ID)
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

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
}

func TestDestroy_RemovesTheDirectoryEvenWhenTheRuntimeFails(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)
	f.runtime.FailOn("Stop", errors.New("daemon is wedged"))
	f.runtime.FailOn("Remove", errors.New("daemon is wedged"))

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
		f.runtime.Seed(id)
		require.NoError(t, os.MkdirAll(f.layout.ContainerDir(id), 0o777))
	}
	// An unrelated container must survive.
	f.runtime.Seed("some-other-service")

	require.NoError(t, f.manager.CleanupOrphans(context.Background()))

	for _, id := range orphans {
		state, ok := f.runtime.Get(id)
		require.True(t, ok)
		assert.True(t, state.Removed, "%s should have been reclaimed", id)
		assert.NoDirExists(t, f.layout.ContainerDir(id))
	}

	other, ok := f.runtime.Get("some-other-service")
	require.True(t, ok)
	assert.False(t, other.Removed, "containers this worker does not own must be left alone")
}

func TestCleanupOrphans_NoOpWhenThereAreNone(t *testing.T) {
	f := newFixture(t)
	require.NoError(t, f.manager.CleanupOrphans(context.Background()))
}

func TestCleanupOrphans_SurfacesListFailures(t *testing.T) {
	f := newFixture(t)
	f.runtime.FailOn("List", sandbox.ErrRuntimeUnavailable)

	err := f.manager.CleanupOrphans(context.Background())
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrRuntimeUnavailable)
}

// newFixtureWithPauseTTL is newFixture with a nonzero Spec.PauseTTL, so the
// reaper has something to expire.
func newFixtureWithPauseTTL(t *testing.T, ttl time.Duration) *fixture {
	t.Helper()
	_, artifacts := buildArtifacts(t, "checkpoint_1")
	return newFixtureWithSpec(t, artifacts, container.Spec{
		NamePrefix:   "exec_container-",
		CPUQuota:     50000,
		CPUPeriod:    100000,
		PidsLimit:    100,
		CgroupParent: "/readie",
		DirPerm:      0o777,
		PauseTTL:     ttl,
	})
}

func TestReapExpiredPauses_DestroysAContainerPastItsTTL(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	f.clock.Advance(time.Minute)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
	assert.NoDirExists(t, f.layout.ContainerDir(h.ID))
}

func TestReapExpiredPauses_LeavesAContainerBeforeItsTTL(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	f.clock.Advance(30 * time.Second)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.False(t, state.Removed)
	assert.True(t, state.Paused)
}

func TestReapExpiredPauses_ResumingClearsTheTimer(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	f.clock.Advance(30 * time.Second)
	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{ContainerID: h.ID})
	require.NoError(t, err)

	// More time has now passed since the original pause than the TTL, but the
	// container was resumed in between and never re-paused.
	f.clock.Advance(time.Minute)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.False(t, state.Removed, "a resumed container must not be reaped for its former pause")
}

func TestReapExpiredPauses_RepausingResetsTheTimerToFullTTL(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	f.clock.Advance(30 * time.Second)
	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{ContainerID: h.ID})
	require.NoError(t, err)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	// Only half the TTL has elapsed since the second pause, even though more
	// than a full TTL has elapsed since the first.
	f.clock.Advance(30 * time.Second)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))
	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.False(t, state.Removed, "the timer must have restarted at the second pause")

	f.clock.Advance(30 * time.Second)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))
	state, ok = f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed, "the full TTL has now elapsed since the second pause")
}

func TestReapExpiredPauses_NonPositiveTTLDisablesReaping(t *testing.T) {
	f := newFixtureWithPauseTTL(t, 0)
	h := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID))

	f.clock.Advance(24 * time.Hour)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.False(t, state.Removed)
}

func TestReapExpiredPauses_NoOpWhenNothingIsPaused(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))
}

func TestReapExpiredPauses_DestroysOnlyTheExpiredOnes(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Minute)

	older := acquireNew(t, f)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, older.ID))

	f.clock.Advance(45 * time.Second)

	newer, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(1 << 20),
	})
	require.NoError(t, err)
	require.NoError(t, f.manager.Pause(context.Background(), registry.ExecutionRef{}, newer.ID))

	// 45s since older's pause, 15s since newer's - only older has crossed the
	// one-minute TTL.
	f.clock.Advance(15 * time.Second)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))

	olderState, ok := f.runtime.Get(older.ID)
	require.True(t, ok)
	assert.True(t, olderState.Removed)

	newerState, ok := f.runtime.Get(newer.ID)
	require.True(t, ok)
	assert.False(t, newerState.Removed)
	assert.True(t, newerState.Paused)
}

func TestReapExpiredPauses_IsSafeUnderConcurrentPauseAndReap(t *testing.T) {
	f := newFixtureWithPauseTTL(t, time.Millisecond)

	var wg sync.WaitGroup
	for range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
				Alloc: memAlloc(1 << 20),
			})
			if err != nil {
				return
			}
			_ = f.manager.Pause(context.Background(), registry.ExecutionRef{}, h.ID)
			_ = f.manager.ReapExpiredPauses(context.Background())
		}()
	}
	wg.Wait()

	f.clock.Advance(time.Hour)
	require.NoError(t, f.manager.ReapExpiredPauses(context.Background()))
}

func TestInspect_ReportsTheAppliedAllocation(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)

	inspected, err := f.manager.Inspect(context.Background(), h.ID, "checkpoint_1")
	require.NoError(t, err)

	assert.Equal(t, h.ID, inspected.ID)
	assert.Equal(t, "checkpoint_1", inspected.CheckpointID)
	assert.Equal(t, int64(512<<20), inspected.Alloc.Memory().Alloc)
}

func TestNewManager_RejectsMissingDependencies(t *testing.T) {
	_, err := container.NewManager(container.ManagerDeps{})
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrInvalidDeps)

	for _, want := range []string{"Runtime", "Reporter", "Layout", "Names", "FS", "Artifacts"} {
		assert.Contains(t, err.Error(), want)
	}
}

// A router outage degrades scheduling but must never fail an execution.
func TestManager_TreatsRouterFailuresAsNonFatal(t *testing.T) {
	f := newFixture(t)
	f.registry.FailOn("ExecutorStatus", errors.New("router is down"))

	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(1 << 20),
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

	// Leave it paused the way a completed, session-bound execution does.
	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{SessionID: "sess-1"}, h, container.OutcomeSuccess))
	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	require.True(t, state.Paused)

	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	state, ok = f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
	assert.False(t, state.Paused)
}

// Unpausing a container that is not paused is a conflict, and must not be
// treated as a failure of the destroy.
func TestDestroy_TolerantOfAnUnpausableContainer(t *testing.T) {
	f := newFixture(t)
	h := acquireNew(t, f)
	f.runtime.FailOn("Unpause", sandbox.ErrConflict)

	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	state, ok := f.runtime.Get(h.ID)
	require.True(t, ok)
	assert.True(t, state.Removed)
}

// The stop timeout must stay well inside a supervisor's shutdown grace period.
func TestSpecFromConfig_UsesAShortContainerStopTimeout(t *testing.T) {
	cfg, err := config.Load(func(k string) string {
		return map[string]string{
			"SERVICE_NAME": "worker", "PORT": "50052", "WORKER_DIR": "/shared",
			"ROUTER_URI": "router:50051", "ARTIFACT_ROOT": "/var/lib/readie",
		}[k]
	})
	require.NoError(t, err)

	spec := container.SpecFromConfig(cfg)
	assert.Positive(t, spec.StopTimeout)
	assert.LessOrEqual(t, spec.StopTimeout, 5*time.Second,
		"reclamation runs during shutdown, inside the supervisor's grace period")
}

// Load answers "how much of this worker is spoken for". Nothing depends on it
// for correctness, but the router schedules on it, so an over- or under-count
// silently skews placement across the fleet.

func TestLoad_IsZeroOnAFreshManager(t *testing.T) {
	f := newFixture(t)

	count, reserved := f.manager.Load()

	assert.Zero(t, count)
	assert.Zero(t, reserved)
}

func TestLoad_CountsAcquiredContainersAndTheirReservations(t *testing.T) {
	f := newFixture(t)

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(512 << 20),
	})
	require.NoError(t, err)
	_, err = f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(256 << 20),
	})
	require.NoError(t, err)

	count, reserved := f.manager.Load()

	assert.Equal(t, int32(2), count)
	assert.Equal(t, int64(768<<20), reserved)
}

func TestLoad_DropsAContainerOnRelease(t *testing.T) {
	f := newFixture(t)
	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(512 << 20),
	})
	require.NoError(t, err)

	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess))

	count, reserved := f.manager.Load()
	assert.Zero(t, count)
	assert.Zero(t, reserved)
}

// A paused container holds pages but is reserved for nobody. Counting it as
// occupied would strand the warm containers the whole system exists to reuse.
func TestLoad_ExcludesAPausedContainerWaitingInTheWarmPool(t *testing.T) {
	f := newFixture(t)
	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(512 << 20),
	})
	require.NoError(t, err)
	require.NoError(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{SessionID: "sess-1"}, h, container.OutcomeSuccess))

	// Resuming it makes it occupied again.
	resumed, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		ContainerID: h.ID,
		Alloc:       memAlloc(512 << 20),
	})
	require.NoError(t, err)

	count, _ := f.manager.Load()
	assert.Equal(t, int32(1), count)
	assert.Equal(t, h.ID, resumed.ID)
}

// Release runs on the failure path too, and a pause that fails must not leave
// the container reserved forever - that leaks capacity for the process's life.
func TestLoad_DropsAContainerEvenWhenReleaseFails(t *testing.T) {
	f := newFixture(t)
	h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		Alloc: memAlloc(512 << 20),
	})
	require.NoError(t, err)

	f.runtime.FailOn("Pause", errors.New("runtime is wedged"))
	require.Error(t, f.manager.Release(
		context.Background(), registry.ExecutionRef{SessionID: "sess-1"}, h, container.OutcomeSuccess))

	count, _ := f.manager.Load()
	assert.Zero(t, count)
}

func TestLoad_IsSafeUnderConcurrentAcquireAndRelease(t *testing.T) {
	f := newFixture(t)

	var wg sync.WaitGroup
	for range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			h, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
				Alloc: memAlloc(1 << 20),
			})
			if err != nil {
				return
			}
			_, _ = f.manager.Load()
			_ = f.manager.Release(
				context.Background(), registry.ExecutionRef{}, h, container.OutcomeSuccess)
		}()
	}
	wg.Wait()

	count, reserved := f.manager.Load()
	assert.Zero(t, count)
	assert.Zero(t, reserved)
}

// activeRootfs is the rootfs every sandbox in this fixture runs against.
func activeRootfs(t *testing.T, f *fixture) string {
	t.Helper()
	rootfs, err := f.artifacts.Rootfs()
	require.NoError(t, err)
	return rootfs
}

// CanonicalSpec is what the worker fingerprints itself against at startup. It
// must hash identically to a real container's spec, or a healthy worker would
// wrongly refuse its own checkpoints. Reusing createSpec is what guarantees it,
// and this proves the guarantee through the real Fingerprint.
func TestCanonicalSpec_FingerprintsIdenticallyToAContainer(t *testing.T) {
	f := newFixture(t)
	acquireNew(t, f)
	real := f.runtime.CreateSpecs()[0]

	canonical := f.manager.CanonicalSpec(activeRootfs(t, f))

	realSpec, err := runsc.BuildSpec(real)
	require.NoError(t, err)
	canonicalSpec, err := runsc.BuildSpec(canonical)
	require.NoError(t, err)

	const overlay, network, gpu = "root:memory", "none", false
	assert.Equal(t,
		runsc.Fingerprint(realSpec, overlay, network, gpu),
		runsc.Fingerprint(canonicalSpec, overlay, network, gpu))
}

// A worker can now start with no root filesystem. It cannot create a container,
// and the failure has to name the missing artifact rather than surface from
// somewhere inside runsc.

// emptyArtifacts is a registry over an artifact root that holds nothing.
func emptyArtifacts(t *testing.T) container.Artifacts {
	t.Helper()
	registry, err := artifact.Load(artifact.Options{Root: t.TempDir(), Log: logging.Discard()})
	require.NoError(t, err)
	return registry
}

func TestAcquire_WithoutARootfsFailsBeforeTouchingTheRuntime(t *testing.T) {
	f := newFixtureWith(t, emptyArtifacts(t))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{})

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrNoArtifacts)
	assert.ErrorIs(t, err, container.ErrAcquireFailed)
	assert.Empty(t, f.runtime.CreateSpecs(),
		"nothing may reach the runtime: there is no rootfs to hand it")
}

func TestAcquire_WithoutARootfsLeavesNoContainerDirectoryBehind(t *testing.T) {
	// Failing before the directory is created is what keeps a worker in this
	// state from slowly filling its disk with one directory per rejected
	// request.
	f := newFixtureWith(t, emptyArtifacts(t))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{})

	require.Error(t, err)
	count, reserved := f.manager.Load()
	assert.Zero(t, count)
	assert.Zero(t, reserved)
}

func TestAcquire_AnUnknownCheckpointWithoutARootfsReportsTheMissingArtifact(t *testing.T) {
	// "unknown checkpoint" would be true but useless here: the reason nothing
	// can run is that there is no root filesystem at all.
	f := newFixtureWith(t, emptyArtifacts(t))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{
		CheckpointID: "checkpoint_1",
	})

	require.Error(t, err)
	assert.ErrorIs(t, err, artifact.ErrNoArtifacts)
	assert.NotErrorIs(t, err, artifact.ErrUnknownCheckpoint)
}

// --- Sandbox network provisioning -----------------------------------------

func newFixtureWithFakeNetwork(t *testing.T) (*fixture, *fakenetwork.Provisioner) {
	t.Helper()
	_, artifacts := buildArtifacts(t, "checkpoint_1")
	net := fakenetwork.New()
	f := newFixtureWithNetwork(t, artifacts, container.Spec{
		NamePrefix:   "exec_container-",
		CPUQuota:     50000,
		CPUPeriod:    100000,
		PidsLimit:    100,
		CgroupParent: "/readie",
		DirPerm:      0o777,
	}, net)
	return f, net
}

func TestAcquire_ProvisionsTheNetworkBeforeCreatingTheContainer(t *testing.T) {
	f, net := newFixtureWithFakeNetwork(t)

	h := acquireNew(t, f)

	assert.Equal(t, []string{h.ID}, net.Provisions())
	spec := f.runtime.CreateSpecs()[0]
	alloc := net.Allocations()[h.ID]
	require.NotEmpty(t, alloc.NetnsPath)
	assert.Equal(t, alloc.NetnsPath, spec.NetnsPath,
		"the spec the runtime receives must carry the network's own allocated path")
}

func TestAcquire_APortProvisionFailureNeverReachesTheRuntime(t *testing.T) {
	f, net := newFixtureWithFakeNetwork(t)
	net.FailOn("Provision", errors.New("no free network slot"))

	_, err := f.manager.Acquire(context.Background(), container.AcquireRequest{})

	require.Error(t, err)
	assert.Empty(t, f.runtime.CreateSpecs(), "a failed provision must never reach the runtime")
	count, reserved := f.manager.Load()
	assert.Zero(t, count)
	assert.Zero(t, reserved)
}

func TestDestroy_ReleasesTheNetworkAfterRemove(t *testing.T) {
	f, net := newFixtureWithFakeNetwork(t)
	h := acquireNew(t, f)

	require.NoError(t, f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID))

	assert.Equal(t, []string{h.ID}, net.Releases())
	assert.Empty(t, net.Allocations(), "the released allocation must no longer be tracked as live")
}

func TestDestroy_ReleasesTheNetworkEvenWhenTheRuntimeFails(t *testing.T) {
	f, net := newFixtureWithFakeNetwork(t)
	h := acquireNew(t, f)
	f.runtime.FailOn("Stop", errors.New("daemon is wedged"))
	f.runtime.FailOn("Remove", errors.New("daemon is wedged"))

	err := f.manager.Destroy(context.Background(), registry.ExecutionRef{}, h.ID)
	require.Error(t, err)

	assert.Equal(t, []string{h.ID}, net.Releases(),
		"a wedged runtime must not leak the network too")
}

func TestCanonicalSpec_NeverProvisionsARealNetwork(t *testing.T) {
	f, net := newFixtureWithFakeNetwork(t)

	_ = f.manager.CanonicalSpec(activeRootfs(t, f))

	assert.Empty(t, net.Provisions(), "no real sandbox runs under the fingerprint probe")
}

func TestCanonicalSpec_FingerprintsIdenticallyToAContainerWithARealNetwork(t *testing.T) {
	f, _ := newFixtureWithFakeNetwork(t)
	acquireNew(t, f)
	real := f.runtime.CreateSpecs()[0]
	require.NotEmpty(t, real.NetnsPath, "the real container must have gotten a provisioned netns path")

	canonical := f.manager.CanonicalSpec(activeRootfs(t, f))
	require.Empty(t, canonical.NetnsPath)

	realSpec, err := runsc.BuildSpec(real)
	require.NoError(t, err)
	canonicalSpec, err := runsc.BuildSpec(canonical)
	require.NoError(t, err)

	const overlay, network, gpu = "root:memory", "sandbox", false
	assert.Equal(t,
		runsc.Fingerprint(realSpec, overlay, network, gpu),
		runsc.Fingerprint(canonicalSpec, overlay, network, gpu),
		"a real, per-sandbox netns path must never change the fingerprint")
}
