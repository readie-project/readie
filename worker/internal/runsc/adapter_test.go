package runsc

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	specs "github.com/opencontainers/runtime-spec/specs-go"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/readie/worker/internal/logging"
	"github.com/illinoisdata/readie/worker/internal/sandbox"
)

const testID = "exec_container-abc"

type fixture struct {
	adapter *Adapter
	runner  *fakeRunner
	root    string
	bundles string
}

func newFixture(t *testing.T, mutate ...func(*Options)) *fixture {
	t.Helper()

	base := t.TempDir()
	root := filepath.Join(base, "state")
	bundles := filepath.Join(base, "sandboxes")
	require.NoError(t, os.MkdirAll(root, 0o755))
	require.NoError(t, os.MkdirAll(bundles, 0o755))

	runner := newFakeRunner()
	opts := Options{
		Binary: "/usr/local/bin/runsc", Root: root, BundlesDir: bundles,
		Network: "none", HostUDS: "create", Overlay: "root:memory",
		Runner: runner, Log: logging.Discard(),
		Now: func() time.Time { return time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC) },
	}
	for _, m := range mutate {
		m(&opts)
	}

	adapter, err := New(opts)
	require.NoError(t, err)

	return &fixture{adapter: adapter, runner: runner, root: root, bundles: bundles}
}

func testCreateSpec(f *fixture) sandbox.CreateSpec {
	return sandbox.CreateSpec{
		ID:         testID,
		BundleDir:  filepath.Join(f.bundles, testID),
		RootfsPath: "/opt/executor-rootfs",
		Args:       []string{"python", "-u", "/app/executor/app.py"},
		Env:        []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/lib/python3.12/dist-packages"},
		Cwd:        "/",
		Mounts: []sandbox.Mount{
			{Source: "/shared/" + testID, Destination: "/tmp", Type: "bind", Options: []string{"rbind", "rw"}},
		},
		MemoryBytes: 512 << 20,
		CPUQuota:    50000,
		CPUPeriod:   100000,
		PidsLimit:   100,
		CgroupsPath: "/readie/" + testID,
	}
}

// markStateExists makes the runtime state directory report the sandbox as
// present, which is how the adapter distinguishes "gone" from "failed".
func (f *fixture) markStateExists(t *testing.T, id string) {
	t.Helper()
	require.NoError(t, os.MkdirAll(filepath.Join(f.root, id), 0o755))
}

func (f *fixture) readBundleSpec(t *testing.T, id string) *specs.Spec {
	t.Helper()
	spec, err := readSpec(filepath.Join(f.bundles, id, configFileName))
	require.NoError(t, err)
	return spec
}

// ---------------------------------------------------------------------------
// argv construction
// ---------------------------------------------------------------------------

// runsc parses flags with stdlib flag semantics, which stop at the first
// non-flag argument. Global flags must precede the subcommand and subcommand
// flags must follow it; getting that wrong is misread rather than rejected, so
// argv is asserted whole rather than by substring.
//
// Start launches coldStart/restore in a goroutine - run and restore otherwise
// block in the foreground for the sandbox's entire lifetime, which would wedge
// Start for as long as the sandbox lives - so every assertion on what the
// runner recorded has to wait for that goroutine rather than read argv the
// instant Start returns.
func TestColdStart_BuildsTheExactArgv(t *testing.T) {
	f := newFixture(t)

	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	assert.Empty(t, f.runner.argv(), "Create writes the bundle only; it starts no process")

	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{}))

	bundle := filepath.Join(f.bundles, testID)
	require.Eventually(t, func() bool {
		return len(f.runner.argv()) > 0
	}, time.Second, time.Millisecond, "coldStart never spawned run")

	assert.Equal(t, [][]string{
		{
			"--root=" + f.root, "--network=none", "--host-uds=create", "--overlay2=root:memory",
			"run", "--bundle=" + bundle, "--pid-file=" + filepath.Join(bundle, pidFileName), testID,
		},
	}, f.runner.argv())
}

func TestRestore_BuildsTheExactArgv(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	image := filepath.Join(t.TempDir(), "checkpoint_1")
	require.NoError(t, os.MkdirAll(image, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(image, "checkpoint.img"), []byte("image"), 0o644))

	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{
		CheckpointID: "checkpoint_1", CheckpointDir: image,
	}))

	bundle := filepath.Join(f.bundles, testID)
	require.Eventually(t, func() bool {
		return len(f.runner.argv()) > 0
	}, time.Second, time.Millisecond, "restore was never spawned")

	assert.Equal(t, [][]string{{
		"--root=" + f.root, "--network=none", "--host-uds=create", "--overlay2=root:memory",
		"restore", "--detach", "--bundle=" + bundle, "--image-path=" + image,
		"--pid-file=" + filepath.Join(bundle, pidFileName), testID,
	}}, f.runner.argv(), "restore blocks in the foreground for the sandbox's life regardless of --detach")
}

func TestGlobalFlags_OptionalOnesAreOmittedWhenUnset(t *testing.T) {
	f := newFixture(t, func(o *Options) {
		o.HostUDS = ""
		o.Overlay = ""
		o.Platform = ""
	})

	require.NoError(t, f.adapter.Pause(context.Background(), testID))
	assert.Equal(t, []string{"--root=" + f.root, "--network=none", "pause", testID}, f.runner.argv()[0])
}

func TestGlobalFlags_IncludeCgroupAndDebugSettings(t *testing.T) {
	f := newFixture(t, func(o *Options) {
		o.IgnoreCgroups = true
		o.Debug = true
		o.DebugLogDir = "/var/log/runsc" // no trailing slash on purpose
		o.Platform = "systrap"
	})

	require.NoError(t, f.adapter.Pause(context.Background(), testID))

	assert.Equal(t, []string{
		"--root=" + f.root, "--network=none", "--host-uds=create", "--overlay2=root:memory",
		"--platform=systrap", "--ignore-cgroups",
		"--debug", "--debug-log=/var/log/runsc/", "--log-format=json",
		"pause", testID,
	}, f.runner.argv()[0],
		"the debug log path needs a trailing slash or every sandbox appends to one file")
}

func TestCheckpoint_BuildsTheExactArgv(t *testing.T) {
	f := newFixture(t)
	f.runner.stdout["state"] = []byte(`{"ociVersion":"1.0.2","id":"` + testID + `","pid":42,"status":"running"}`)

	dir := filepath.Join(t.TempDir(), "checkpoint_7")
	require.NoError(t, f.adapter.Checkpoint(context.Background(), testID, sandbox.CheckpointSpec{
		Dir: dir, LeaveRunning: true,
	}))

	assert.DirExists(t, dir, "the runtime will not create the image directory itself")
	assert.Equal(t, []string{
		"--root=" + f.root, "--network=none", "--host-uds=create", "--overlay2=root:memory",
		"checkpoint", "--image-path=" + dir, "--leave-running", testID,
	}, f.runner.argvFor("checkpoint"))
}

// ---------------------------------------------------------------------------
// bundle generation
// ---------------------------------------------------------------------------

// The generated spec is a contract with both the executor and every existing
// checkpoint, so each load-bearing field is asserted individually.
func TestCreate_WritesTheExecutorContract(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	spec := f.readBundleSpec(t, testID)

	assert.Equal(t, []string{"python", "-u", "/app/executor/app.py"}, spec.Process.Args)
	assert.False(t, spec.Process.Terminal,
		"a pty would need --console-socket and would break log capture")
	assert.Contains(t, spec.Process.Env, "EXECUTOR_DIR=/tmp")
	assert.Contains(t, spec.Process.Env, "PYTHONPATH=/lib/python3.12/dist-packages")
	assert.Equal(t, "/opt/executor-rootfs", spec.Root.Path)

	// The socket bind must be last, after the standard mount set.
	last := spec.Mounts[len(spec.Mounts)-1]
	assert.Equal(t, "/tmp", last.Destination)
	assert.Equal(t, "bind", last.Type)
	assert.Equal(t, filepath.Join("/shared", testID), last.Source)

	require.NotNil(t, spec.Linux.Resources.Memory)
	assert.Equal(t, int64(512<<20), *spec.Linux.Resources.Memory.Limit)
	require.NotNil(t, spec.Linux.Resources.CPU)
	assert.Equal(t, int64(50000), *spec.Linux.Resources.CPU.Quota)
	assert.Equal(t, uint64(100000), *spec.Linux.Resources.CPU.Period)
	require.NotNil(t, spec.Linux.Resources.Pids)
	assert.Equal(t, int64(100), *spec.Linux.Resources.Pids.Limit)
	assert.Equal(t, "/readie/"+testID, spec.Linux.CgroupsPath)
}

func TestCreate_RejectsAnUnusableSpec(t *testing.T) {
	f := newFixture(t)

	tests := map[string]func(*sandbox.CreateSpec){
		"no id":              func(s *sandbox.CreateSpec) { s.ID = "" },
		"no rootfs":          func(s *sandbox.CreateSpec) { s.RootfsPath = "" },
		"relative rootfs":    func(s *sandbox.CreateSpec) { s.RootfsPath = "rootfs" },
		"no args":            func(s *sandbox.CreateSpec) { s.Args = nil },
		"relative mount dst": func(s *sandbox.CreateSpec) { s.Mounts[0].Destination = "tmp" },
		"relative bind src":  func(s *sandbox.CreateSpec) { s.Mounts[0].Source = "shared" },
	}

	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			spec := testCreateSpec(f)
			spec.Mounts = append([]sandbox.Mount(nil), spec.Mounts...)
			mutate(&spec)

			_, err := f.adapter.Create(context.Background(), spec)
			require.Error(t, err)
			assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)
		})
	}
}

// A child mounted before its parent is shadowed when the parent lands on top,
// which presents as a mysteriously empty directory rather than an error.
func TestCreate_RejectsChildMountBeforeItsParent(t *testing.T) {
	f := newFixture(t)
	spec := testCreateSpec(f)
	spec.Mounts = []sandbox.Mount{
		{Source: "/a/child", Destination: "/tmp/inner", Type: "bind", Options: []string{"rbind"}},
		{Source: "/a/parent", Destination: "/tmp", Type: "bind", Options: []string{"rbind"}},
	}

	_, err := f.adapter.Create(context.Background(), spec)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)
	assert.Contains(t, err.Error(), "before its child")
}

func TestCreate_RejectsDuplicateMountDestinations(t *testing.T) {
	f := newFixture(t)
	spec := testCreateSpec(f)
	spec.Mounts = append(spec.Mounts, sandbox.Mount{
		Source: "/other", Destination: "/tmp", Type: "bind", Options: []string{"rbind"},
	})

	_, err := f.adapter.Create(context.Background(), spec)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)
}

// ---------------------------------------------------------------------------
// fingerprint
// ---------------------------------------------------------------------------

// The fingerprint must survive the things that legitimately vary between two
// workers restoring the same checkpoint, and must change for the things that
// genuinely break a restore.
func TestFingerprint_IgnoresWhatVariesPerRequest(t *testing.T) {
	f := newFixture(t)

	base, err := BuildSpec(testCreateSpec(f))
	require.NoError(t, err)
	want := Fingerprint(base, "root:memory", "none", false)

	t.Run("memory limit", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.MemoryBytes = 1 << 30
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.Equal(t, want, Fingerprint(other, "root:memory", "none", false),
			"the allocation varies per request and must not invalidate checkpoints")
	})

	t.Run("mount source", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.Mounts[0].Source = "/somewhere/else"
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.Equal(t, want, Fingerprint(other, "root:memory", "none", false),
			"sources are resolved fresh at restore, which is what makes a checkpoint portable")
	})

	t.Run("cgroups path", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.CgroupsPath = "/elsewhere"
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.Equal(t, want, Fingerprint(other, "root:memory", "none", false))
	})

	// Load-bearing: a provisioned network namespace's path is unique per
	// sandbox (and can differ across a restore of the same checkpoint), so it
	// must never affect the fingerprint the way the namespace's mere presence
	// does.
	t.Run("netns path", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.NetnsPath = "/var/run/netns/exec_container-abc"
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.Equal(t, want, Fingerprint(other, "root:memory", "none", false),
			"a provisioned netns path is unique per sandbox and must not invalidate checkpoints")
	})

	// Load-bearing, not incidental. The offline pipeline distinguishes one
	// checkpoint from another purely by READIE_PREIMPORT in the environment, so
	// every checkpoint in a generation has to share a fingerprint or the worker
	// would refuse all but the one it happened to compute against. Adding env
	// to the fingerprint would break every existing generation at once and no
	// other test would notice.
	t.Run("environment", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.Env = append(append([]string(nil), spec.Env...), "READIE_PREIMPORT=pandas,numpy")
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.Equal(t, want, Fingerprint(other, "root:memory", "none", false),
			"pre-imports travel in the environment and must not change the fingerprint")
	})
}

func TestFingerprint_ChangesForWhatBreaksARestore(t *testing.T) {
	f := newFixture(t)

	base, err := BuildSpec(testCreateSpec(f))
	require.NoError(t, err)
	want := Fingerprint(base, "root:memory", "none", false)

	t.Run("mount added", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.Mounts = append(spec.Mounts, sandbox.Mount{
			Source: "/extra", Destination: "/opt/extra", Type: "bind", Options: []string{"rbind", "ro"},
		})
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.NotEqual(t, want, Fingerprint(other, "root:memory", "none", false),
			"the runtime reattaches one gofer per mount; a different count cannot be restored")
	})

	t.Run("args", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.Args = []string{"python", "-u", "/other.py"}
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.NotEqual(t, want, Fingerprint(other, "root:memory", "none", false))
	})

	t.Run("root readonly", func(t *testing.T) {
		spec := testCreateSpec(f)
		spec.RootReadonly = true
		other, err := BuildSpec(spec)
		require.NoError(t, err)
		assert.NotEqual(t, want, Fingerprint(other, "root:memory", "none", false))
	})

	t.Run("overlay mode", func(t *testing.T) {
		assert.NotEqual(t, want, Fingerprint(base, "none", "none", false))
	})

	t.Run("network mode", func(t *testing.T) {
		assert.NotEqual(t, want, Fingerprint(base, "root:memory", "host", false))
	})

	t.Run("gpu mode", func(t *testing.T) {
		// A GPU checkpoint must never restore into a CPU sandbox, so nvproxy
		// changes the fingerprint even though it is a runtime flag, not a spec
		// field.
		assert.NotEqual(t, want, Fingerprint(base, "root:memory", "none", true))
	})
}

func TestGlobalFlags_NVProxyIsAddedOnlyWhenEnabled(t *testing.T) {
	off := newFixture(t)
	assert.NotContains(t, off.adapter.global, "--nvproxy")

	on := newFixture(t, func(o *Options) { o.NVProxy = true })
	assert.Contains(t, on.adapter.global, "--nvproxy")
	assert.Contains(t, on.adapter.global, "--nvproxy-docker")
}

func TestBuildSpec_GPUAddsTheNvidiaEnv(t *testing.T) {
	f := newFixture(t)
	spec := testCreateSpec(f)
	spec.GPU = true

	built, err := BuildSpec(spec)
	require.NoError(t, err)

	assert.Contains(t, built.Process.Env, "NVIDIA_VISIBLE_DEVICES=all")
	assert.Contains(t, built.Process.Env, "NVIDIA_DRIVER_CAPABILITIES=compute,utility")
}

func TestBuildSpec_NetnsPathJoinsTheNetworkNamespace(t *testing.T) {
	f := newFixture(t)
	spec := testCreateSpec(f)
	spec.NetnsPath = "/var/run/netns/exec_container-abc"

	built, err := BuildSpec(spec)
	require.NoError(t, err)

	var found bool
	for _, ns := range built.Linux.Namespaces {
		if ns.Type == specs.NetworkNamespace {
			found = true
			assert.Equal(t, spec.NetnsPath, ns.Path)
		}
	}
	assert.True(t, found, "a network namespace entry must be present")
}

func TestBuildSpec_EmptyNetnsPathJoinsNothing(t *testing.T) {
	f := newFixture(t)
	spec := testCreateSpec(f)
	spec.NetnsPath = ""

	built, err := BuildSpec(spec)
	require.NoError(t, err)

	for _, ns := range built.Linux.Namespaces {
		if ns.Type == specs.NetworkNamespace {
			assert.Empty(t, ns.Path, "an unset NetnsPath must leave runsc to create a fresh, empty namespace")
		}
	}
}

func TestFingerprint_IsStable(t *testing.T) {
	f := newFixture(t)
	spec, err := BuildSpec(testCreateSpec(f))
	require.NoError(t, err)

	first := Fingerprint(spec, "root:memory", "none", false)
	assert.Equal(t, first, Fingerprint(spec, "root:memory", "none", false))
	assert.Contains(t, first, "sha256:")
}

// ---------------------------------------------------------------------------
// lifecycle behaviour
// ---------------------------------------------------------------------------

// The runtime creates the sandbox before restoring into it, so a partial
// failure leaves state behind. Without cleanup the caller's cold-start retry
// hits "already exists" and a recoverable downgrade becomes a hard failure.
//
// Start no longer reports this failure itself - it returns before restore has
// even run, let alone failed - so what's observable here is the cleanup
// side effect, not Start's return value. A caller learns of the failure only
// by the sandbox never coming up (its dial times out); that's a real gap this
// design accepts, not one this test can paper over.
func TestRestore_CleansUpSoAColdStartCanFollow(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	image := filepath.Join(t.TempDir(), "checkpoint_1")
	require.NoError(t, os.MkdirAll(image, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(image, "checkpoint.img"), []byte("x"), 0o644))
	f.runner.fail["restore"] = commandErr("restore", "failed to restore", 1)

	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{
		CheckpointID: "checkpoint_1", CheckpointDir: image,
	}))

	require.Eventually(t, func() bool {
		return slices.Contains(f.runner.commands(), "delete")
	}, time.Second, time.Millisecond,
		"a partially restored sandbox must be removed before the caller retries")

	// The downgrade the container manager performs must now succeed.
	f.runner.reset()
	delete(f.runner.fail, "restore")
	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{}))
	require.Eventually(t, func() bool {
		return len(f.runner.commands()) > 0
	}, time.Second, time.Millisecond, "coldStart never ran")
	assert.Equal(t, []string{"run"}, f.runner.commands())
}

// Discovering an unusable image costs a full restore timeout unless it is
// checked first.
func TestRestore_RejectsAnEmptyImageWithoutSpawningAnything(t *testing.T) {
	f := newFixture(t)
	empty := t.TempDir()

	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{
		CheckpointID: "checkpoint_1", CheckpointDir: empty,
	}))

	require.Never(t, func() bool {
		return len(f.runner.argv()) > 0
	}, 200*time.Millisecond, 10*time.Millisecond,
		"no process should be started for an unusable image")
}

func TestColdStart_CleansUpWhenStartFails(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	f.runner.fail["run"] = commandErr("run", "boom", 1)

	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{}))

	require.Eventually(t, func() bool {
		return slices.Contains(f.runner.commands(), "delete")
	}, time.Second, time.Millisecond, "a failed run must be cleaned up")
	assert.Equal(t, []string{"run", "delete"}, f.runner.commands())
}

func TestStart_AttachesTheLogFileAsRealDescriptors(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	require.NoError(t, f.adapter.Start(context.Background(), testID, sandbox.StartSpec{}))

	require.Eventually(t, func() bool {
		return f.runner.lastStdio().Out != nil
	}, time.Second, time.Millisecond, "run was never spawned with stdio attached")

	stdio := f.runner.lastStdio()
	require.NotNil(t, stdio.Out, "the sandbox inherits these; they cannot be pipes we own")
	assert.Equal(t, f.adapter.LogPath(testID), stdio.Out.Name())
	assert.Equal(t, stdio.Out.Name(), stdio.Err.Name(), "stdout and stderr share one log")
	assert.FileExists(t, f.adapter.LogPath(testID))
}

func TestInspect_ReportsStateAndTheBundleAllocation(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	f.runner.stdout["state"] = []byte(`{"ociVersion":"1.0.2","id":"` + testID + `","pid":4242,"status":"paused"}`)

	info, err := f.adapter.Inspect(context.Background(), testID)
	require.NoError(t, err)

	assert.Equal(t, testID, info.ID)
	assert.Equal(t, 4242, info.PID)
	assert.True(t, info.Paused)
	assert.True(t, info.Running, "a paused sandbox still exists and holds resources")
	assert.Equal(t, int64(512<<20), info.MemoryBytes,
		"the allocation comes from the bundle, so it survives a worker restart")
}

// runsc has no update subcommand, so the bundle is rewritten instead. Failing
// here would break every warm resume, so a missing cgroup must not error.
func TestUpdate_RewritesTheBundleAndToleratesAnAbsentCgroup(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	require.NoError(t, f.adapter.Update(context.Background(), testID, sandbox.UpdateSpec{
		MemoryBytes: 256 << 20,
	}))

	spec := f.readBundleSpec(t, testID)
	assert.Equal(t, int64(256<<20), *spec.Linux.Resources.Memory.Limit)
	assert.Empty(t, f.runner.argv(), "there is no runsc update subcommand to call")
}

func TestUpdate_ReportsAnAbsentBundleAsNotFound(t *testing.T) {
	f := newFixture(t)
	err := f.adapter.Update(context.Background(), "never-created", sandbox.UpdateSpec{MemoryBytes: 1 << 20})
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrNotFound)
}

// ---------------------------------------------------------------------------
// listing and cleanup
// ---------------------------------------------------------------------------

// Enumerating bundles rather than asking the runtime is what finds sandboxes
// whose runtime state was lost - exactly the orphans cleanup exists for, and
// which a runtime listing would not mention.
func TestList_FindsBundlesWhoseRuntimeStateIsGone(t *testing.T) {
	f := newFixture(t)
	f.runner.fail["state"] = commandErr("state", "container does not exist", 1)

	for _, id := range []string{"exec_container-a", "exec_container-b", "unrelated"} {
		require.NoError(t, os.MkdirAll(filepath.Join(f.bundles, id), 0o755))
	}

	summaries, err := f.adapter.List(context.Background(), "exec_container-")
	require.NoError(t, err)

	require.Len(t, summaries, 2)
	assert.Equal(t, "exec_container-a", summaries[0].ID)
	assert.Equal(t, "exec_container-b", summaries[1].ID)
	assert.Equal(t, "unknown", summaries[0].State)
}

func TestList_ReportsLiveState(t *testing.T) {
	f := newFixture(t)
	require.NoError(t, os.MkdirAll(filepath.Join(f.bundles, testID), 0o755))
	f.markStateExists(t, testID)
	f.runner.stdout["state"] = []byte(`{"id":"` + testID + `","status":"running"}`)

	summaries, err := f.adapter.List(context.Background(), "exec_container-")
	require.NoError(t, err)
	require.Len(t, summaries, 1)
	assert.Equal(t, "running", summaries[0].State)
}

func TestList_EmptyWhenThereAreNoBundles(t *testing.T) {
	f := newFixture(t)
	summaries, err := f.adapter.List(context.Background(), "exec_container-")
	require.NoError(t, err)
	assert.Empty(t, summaries)
}

// A bundle left behind by a lost sandbox would leak disk forever.
func TestRemove_DeletesTheBundleEvenWhenTheRuntimeSaysItIsGone(t *testing.T) {
	t.Skip("removeBundle's os.RemoveAll is currently disabled; re-enable once that's decided")
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	require.DirExists(t, filepath.Join(f.bundles, testID))

	f.runner.fail["delete"] = commandErr("delete", "container does not exist", 1)

	err = f.adapter.Remove(context.Background(), testID, true)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrNotFound)
	assert.NoDirExists(t, filepath.Join(f.bundles, testID))
}

func TestRemove_DeletesTheBundleOnSuccess(t *testing.T) {
	t.Skip("removeBundle's os.RemoveAll is currently disabled; re-enable once that's decided")
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	require.NoError(t, f.adapter.Remove(context.Background(), testID, true))
	assert.NoDirExists(t, filepath.Join(f.bundles, testID))
	assert.Equal(t, []string{
		"--root=" + f.root, "--network=none", "--host-uds=create", "--overlay2=root:memory",
		"delete", "--force", testID,
	}, f.runner.argvFor("delete"))
}

// ---------------------------------------------------------------------------
// error classification
// ---------------------------------------------------------------------------

// The state directory is an authoritative oracle for existence, which removes
// the most fragile part of classifying a runtime failure.
func TestClassify_UsesTheStateDirectoryBeforeStderr(t *testing.T) {
	f := newFixture(t)
	f.runner.fail["pause"] = commandErr("pause", "something inscrutable", 1)

	err := f.adapter.Pause(context.Background(), testID)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrNotFound, "no state directory means the sandbox is gone")

	f.markStateExists(t, testID)
	err = f.adapter.Pause(context.Background(), testID)
	require.Error(t, err)
	assert.NotErrorIs(t, err, sandbox.ErrNotFound,
		"with state present, an unrecognised failure stays unclassified rather than being mislabelled")
}

func TestClassify_RecognisesConflicts(t *testing.T) {
	f := newFixture(t)
	f.markStateExists(t, testID)
	f.runner.fail["resume"] = commandErr("resume", "container is not paused", 1)

	err := f.adapter.Unpause(context.Background(), testID)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrConflict)
}

func TestClassify_MissingBinaryIsRuntimeUnavailable(t *testing.T) {
	f := newFixture(t)
	f.markStateExists(t, testID)
	f.runner.fail["pause"] = commandErr("pause", "", -1)

	err := f.adapter.Pause(context.Background(), testID)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrRuntimeUnavailable)
}

func TestProbe_PreparesItsDirectories(t *testing.T) {
	base := t.TempDir()
	runner := newFakeRunner()
	runner.stdout["--version"] = []byte("runsc version release-20250107.0\nspec: 1.1.0-rc.1\n")

	adapter, err := New(Options{
		Root:       filepath.Join(base, "state"),
		BundlesDir: filepath.Join(base, "sandboxes"),
		Runner:     runner, Log: logging.Discard(),
	})
	require.NoError(t, err)

	require.NoError(t, adapter.Probe(context.Background()))
	assert.DirExists(t, filepath.Join(base, "state"))
	assert.DirExists(t, filepath.Join(base, "sandboxes"))
}

func TestProbe_FailsWhenTheRuntimeIsUnusable(t *testing.T) {
	f := newFixture(t)
	f.runner.fail["--version"] = commandErr("--version", "", -1)

	err := f.adapter.Probe(context.Background())
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrRuntimeUnavailable)
}

func TestNew_RejectsMissingDirectories(t *testing.T) {
	_, err := New(Options{BundlesDir: "/x"})
	assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)

	_, err = New(Options{Root: "/x"})
	assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)
}

// ---------------------------------------------------------------------------
// logs
// ---------------------------------------------------------------------------

func TestLogs_ReadsWhatTheSandboxWrote(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(f.adapter.LogPath(testID), []byte("hello\nworld\n"), 0o644))

	logs, err := f.adapter.Logs(context.Background(), testID, false)
	require.NoError(t, err)
	t.Cleanup(func() { _ = logs.Close() })

	out, err := io.ReadAll(logs)
	require.NoError(t, err)
	assert.Equal(t, "hello\nworld\n", string(out))
}

func TestLogs_AbsentLogIsNotFound(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Logs(context.Background(), "never-started", true)
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrNotFound)
}

// The sandbox holds the log file open for its whole life, so a follower must
// wait for more rather than reporting EOF the instant it catches up - and
// Close must interrupt that wait, or the log pump leaks.
func TestLogs_FollowWaitsForMoreAndCloseInterrupts(t *testing.T) {
	f := newFixture(t)
	_, err := f.adapter.Create(context.Background(), testCreateSpec(f))
	require.NoError(t, err)

	path := f.adapter.LogPath(testID)
	require.NoError(t, os.WriteFile(path, []byte("first\n"), 0o644))

	logs, err := f.adapter.Logs(context.Background(), testID, true)
	require.NoError(t, err)

	buf := make([]byte, 64)
	n, err := logs.Read(buf)
	require.NoError(t, err)
	assert.Equal(t, "first\n", string(buf[:n]))

	// Append after the reader has caught up.
	appendTo(t, path, "second\n")
	n, err = logs.Read(buf)
	require.NoError(t, err)
	assert.Equal(t, "second\n", string(buf[:n]))

	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = logs.Read(buf) // blocks until Close
	}()

	time.AfterFunc(50*time.Millisecond, func() { _ = logs.Close() })
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not unblock a pending Read; the log pump would leak")
	}

	assert.NoError(t, logs.Close(), "Close must be idempotent")
}

func appendTo(t *testing.T, path, s string) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND, 0o644)
	require.NoError(t, err)
	_, err = f.WriteString(s)
	require.NoError(t, err)
	require.NoError(t, f.Close())
}

// ---------------------------------------------------------------------------
// stats
// ---------------------------------------------------------------------------

func TestStats_FirstSampleReportsNoUtilisation(t *testing.T) {
	f := newFixture(t)
	f.runner.stdout["events"] = mustEventJSON(t, 1_000_000_000, 512<<20, 1<<30, 2, 7)

	stream, err := f.adapter.Stats(context.Background(), testID, false)
	require.NoError(t, err)
	t.Cleanup(func() { _ = stream.Close() })

	sample, err := stream.Recv()
	require.NoError(t, err)

	assert.Equal(t, int64(512<<20), sample.MemoryUsage)
	assert.Equal(t, int64(1<<30), sample.MemoryLimit)
	assert.Equal(t, int64(1_000_000_000), sample.CPUTotal)
	assert.Equal(t, int64(2), sample.OnlineCPUs)
	assert.Equal(t, int64(7), sample.Pids)
	assert.Zero(t, sample.CPUPercent(),
		"a cumulative counter with no predecessor is not a rate")
}

func TestStats_OneShotStreamEndsAfterOneSample(t *testing.T) {
	f := newFixture(t)
	f.runner.stdout["events"] = mustEventJSON(t, 1, 1, 1, 1, 1)

	stream, err := f.adapter.Stats(context.Background(), testID, false)
	require.NoError(t, err)
	t.Cleanup(func() { _ = stream.Close() })

	_, err = stream.Recv()
	require.NoError(t, err)

	_, err = stream.Recv()
	assert.ErrorIs(t, err, io.EOF)
}

func TestStats_CloseUnblocksAPendingRecv(t *testing.T) {
	f := newFixture(t, func(o *Options) { o.StatsInterval = time.Hour })
	f.runner.stdout["events"] = mustEventJSON(t, 1, 1, 1, 1, 1)

	stream, err := f.adapter.Stats(context.Background(), testID, true)
	require.NoError(t, err)

	_, err = stream.Recv()
	require.NoError(t, err)

	done := make(chan error, 1)
	go func() {
		_, err := stream.Recv()
		done <- err
	}()

	time.AfterFunc(50*time.Millisecond, func() { _ = stream.Close() })
	select {
	case err := <-done:
		assert.ErrorIs(t, err, io.EOF)
	case <-time.After(5 * time.Second):
		t.Fatal("Close did not unblock Recv; the stats pump would leak")
	}
}

// Wall clock is the denominator because gVisor exposes no host-wide CPU
// counter, so a saturated core must read 100.
func TestCPUPercent_UsesWallClock(t *testing.T) {
	sample := sandbox.Stats{
		CPUTotal: 2_000_000_000, PreCPUTotal: 1_000_000_000, Elapsed: time.Second,
	}
	assert.InDelta(t, 100.0, sample.CPUPercent(), 0.001)

	half := sandbox.Stats{
		CPUTotal: 1_500_000_000, PreCPUTotal: 1_000_000_000, Elapsed: time.Second,
	}
	assert.InDelta(t, 50.0, half.CPUPercent(), 0.001)

	assert.Zero(t, sandbox.Stats{CPUTotal: 5, Elapsed: 0}.CPUPercent())
	assert.Zero(t, sandbox.Stats{CPUTotal: 1, PreCPUTotal: 5, Elapsed: time.Second}.CPUPercent())
}

func mustEventJSON(t *testing.T, cpuTotal, memUsage, memLimit uint64, cpus, pids int) []byte {
	t.Helper()

	percpu := make([]uint64, cpus)
	for i := range percpu {
		percpu[i] = cpuTotal / uint64(cpus)
	}

	raw, err := json.Marshal(event{
		Type: "stats", ID: testID,
		Data: eventData{
			Memory: eventMemory{Usage: eventMemoryEntry{Usage: memUsage, Limit: memLimit}},
			CPU:    eventCPU{Usage: eventCPUUsage{Total: cpuTotal, PerCPU: percpu}},
			Pids:   eventPids{Current: uint64(pids)},
		},
	})
	require.NoError(t, err)
	return raw
}
