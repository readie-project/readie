//go:build linux && runsc_e2e

// This suite runs against a real runsc and is the only place the assumptions
// this adapter makes can actually be checked. It is build-tagged off by
// default because gVisor is Linux-only and works by intercepting syscalls,
// which rules out running it under emulation on a development machine.
//
//	make test-e2e
//
// It needs: Linux/amd64, runsc on PATH, root (or CAP_SYS_ADMIN), and an
// executor rootfs. Point READIE_E2E_ROOTFS at one; the suite skips without it.
//
// Each test here settles one specific uncertainty listed in worker/README.md
// under "Unverified against a real runtime". When one passes, delete the
// corresponding line from that list.
package runsc

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

func e2eRootfs(t *testing.T) string {
	t.Helper()

	rootfs := os.Getenv("READIE_E2E_ROOTFS")
	if rootfs == "" {
		t.Skip("set READIE_E2E_ROOTFS to an executor rootfs to run the real-runsc suite")
	}
	return rootfs
}

func e2eAdapter(t *testing.T, workerDir string) *Adapter {
	t.Helper()

	adapter, err := New(Options{
		Binary:     "runsc",
		Root:       filepath.Join(workerDir, "runsc-state"),
		BundlesDir: filepath.Join(workerDir, "sandboxes"),
		Network:    "none",
		HostUDS:    "create",
		Overlay:    "root:memory",
		Log:        logging.Discard(),
	})
	require.NoError(t, err)
	require.NoError(t, adapter.Probe(context.Background()))
	return adapter
}

// e2eSpec describes a sandbox that binds a socket the host can reach.
func e2eSpec(t *testing.T, adapter *Adapter, workerDir, rootfs, id string, args ...string) sandbox.CreateSpec {
	t.Helper()

	socketDir := filepath.Join(workerDir, id)
	require.NoError(t, os.MkdirAll(socketDir, 0o777))

	return sandbox.CreateSpec{
		ID:         id,
		BundleDir:  adapter.BundleDir(id),
		LogPath:    adapter.LogPath(id),
		RootfsPath: rootfs,
		Args:       args,
		Env:        []string{"EXECUTOR_DIR=/tmp"},
		Cwd:        "/",
		Mounts: []sandbox.Mount{{
			Source: socketDir, Destination: "/tmp", Type: "bind", Options: []string{"rbind", "rw"},
		}},
		MemoryBytes: 512 << 20,
		PidsLimit:   100,
	}
}

// THE test. Nothing in this repository has ever demonstrated that a socket
// bound *inside* a gVisor sandbox is reachable from the host, because the
// offline pipeline captures its checkpoints during the executor's pre-bind
// sleep. The entire executor protocol depends on it.
//
// If this fails, --host-uds is wrong or insufficient, and the fallback is to
// invert the socket direction: the worker listens and the executor connects,
// which changes executor/.
func TestE2E_SandboxBoundSocketIsReachableFromTheHost(t *testing.T) {
	rootfs := e2eRootfs(t)
	workerDir := t.TempDir()
	adapter := e2eAdapter(t, workerDir)

	const id = "e2e-uds"
	spec := e2eSpec(t, adapter, workerDir, rootfs, id,
		"python", "-u", "-c",
		`import socket,os,time
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.bind("/tmp/executor.sock"); s.listen(1)
print("bound",flush=True)
c,_=s.accept()
c.sendall(b"hello-from-the-sandbox"); c.close()
time.sleep(30)`)

	_, err := adapter.Create(context.Background(), spec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), id, true) })

	require.NoError(t, adapter.Start(context.Background(), id, sandbox.StartSpec{}))

	socketPath := filepath.Join(workerDir, id, "executor.sock")
	require.Eventually(t, func() bool {
		_, err := os.Stat(socketPath)
		return err == nil
	}, 30*time.Second, 100*time.Millisecond,
		"the sandbox's socket never appeared on the host: check --host-uds")

	conn, err := net.DialTimeout("unix", socketPath, 5*time.Second)
	require.NoError(t, err, "the socket exists but the host cannot connect to it")
	defer func() { _ = conn.Close() }()

	buf := make([]byte, 64)
	require.NoError(t, conn.SetReadDeadline(time.Now().Add(5*time.Second)))
	n, err := conn.Read(buf)
	require.NoError(t, err)
	assert.Equal(t, "hello-from-the-sandbox", string(buf[:n]))
}

// Verifies that a descriptor handed to `runsc create` survives that command's
// exit and is still written to by the daemonised sandbox — the assumption the
// entire Logs implementation rests on.
func TestE2E_SandboxOutputReachesTheLogFile(t *testing.T) {
	rootfs := e2eRootfs(t)
	workerDir := t.TempDir()
	adapter := e2eAdapter(t, workerDir)

	const id = "e2e-logs"
	spec := e2eSpec(t, adapter, workerDir, rootfs, id,
		"python", "-u", "-c", `import time
for i in range(5):
    print("line", i, flush=True); time.sleep(0.2)
time.sleep(30)`)

	_, err := adapter.Create(context.Background(), spec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), id, true) })
	require.NoError(t, adapter.Start(context.Background(), id, sandbox.StartSpec{}))

	require.Eventually(t, func() bool {
		raw, err := os.ReadFile(adapter.LogPath(id))
		return err == nil && len(raw) > 0
	}, 30*time.Second, 100*time.Millisecond,
		"nothing reached the log: the sandbox did not inherit the descriptor")
}

// Settles the checkpoint/restore round trip, --leave-running, --detach, and
// whether a checkpoint taken by one bundle restores into an identically shaped
// one.
func TestE2E_CheckpointAndRestore(t *testing.T) {
	rootfs := e2eRootfs(t)
	workerDir := t.TempDir()
	adapter := e2eAdapter(t, workerDir)

	const id = "e2e-checkpoint"
	spec := e2eSpec(t, adapter, workerDir, rootfs, id, "python", "-u", "-c", `import time
print("running",flush=True); time.sleep(600)`)

	_, err := adapter.Create(context.Background(), spec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), id, true) })
	require.NoError(t, adapter.Start(context.Background(), id, sandbox.StartSpec{}))

	image := filepath.Join(workerDir, "checkpoints", "e2e_1")
	err = adapter.Checkpoint(context.Background(), id, sandbox.CheckpointSpec{
		Dir: image, LeaveRunning: true,
	})
	require.NoError(t, err, "checkpoint failed: check --leave-running exists")

	entries, err := os.ReadDir(image)
	require.NoError(t, err)
	assert.NotEmpty(t, entries, "the checkpoint produced no image files")

	// Restore into a fresh sandbox built from an identically shaped bundle.
	require.NoError(t, adapter.Remove(context.Background(), id, true))

	const restored = "e2e-restored"
	restoredSpec := e2eSpec(t, adapter, workerDir, rootfs, restored, spec.Args...)
	_, err = adapter.Create(context.Background(), restoredSpec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), restored, true) })

	err = adapter.Start(context.Background(), restored, sandbox.StartSpec{
		CheckpointID: "e2e_1", CheckpointDir: image,
	})
	require.NoError(t, err, "restore failed: check --detach and bundle compatibility")

	info, err := adapter.Inspect(context.Background(), restored)
	require.NoError(t, err)
	assert.True(t, info.Running)
}

// Confirms the events output shape the stats parser assumes, and whether
// gVisor populates a CPU counter at all. If CPU reads zero, the router sees 0%
// utilisation and that is a known limitation rather than a bug.
func TestE2E_StatsReportUsableCounters(t *testing.T) {
	rootfs := e2eRootfs(t)
	workerDir := t.TempDir()
	adapter := e2eAdapter(t, workerDir)

	const id = "e2e-stats"
	spec := e2eSpec(t, adapter, workerDir, rootfs, id, "python", "-u", "-c",
		`x=0
while True: x+=1`)

	_, err := adapter.Create(context.Background(), spec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), id, true) })
	require.NoError(t, adapter.Start(context.Background(), id, sandbox.StartSpec{}))

	stream, err := adapter.Stats(context.Background(), id, true)
	require.NoError(t, err)
	defer func() { _ = stream.Close() }()

	first, err := stream.Recv()
	require.NoError(t, err)
	assert.Positive(t, first.MemoryUsage, "memory usage should be reported")

	second, err := stream.Recv()
	require.NoError(t, err)
	t.Logf("cpu total: %d -> %d over %s (%.1f%%)",
		second.PreCPUTotal, second.CPUTotal, second.Elapsed, second.CPUPercent())
	assert.Positive(t, second.CPUTotal,
		"gVisor reported no CPU counter; utilisation will always read 0%%")
}

// Whether the runtime accepts an absolute root.path. The fallback is a symlink
// inside the bundle.
func TestE2E_AbsoluteRootPathIsAccepted(t *testing.T) {
	rootfs := e2eRootfs(t)
	workerDir := t.TempDir()
	adapter := e2eAdapter(t, workerDir)

	require.True(t, filepath.IsAbs(rootfs))

	const id = "e2e-absroot"
	spec := e2eSpec(t, adapter, workerDir, rootfs, id, "/bin/true")

	_, err := adapter.Create(context.Background(), spec)
	require.NoError(t, err)
	t.Cleanup(func() { _ = adapter.Remove(context.Background(), id, true) })

	assert.NoError(t, adapter.Start(context.Background(), id, sandbox.StartSpec{}))
}

// Records the runtime's own default spec, so BuildSpec's mount set can be
// diffed against it and reconciled before any checkpoint worth keeping is
// generated.
func TestE2E_DumpRunscSpecForComparison(t *testing.T) {
	e2eRootfs(t)

	dir := t.TempDir()
	runner := NewExecRunner("runsc")

	_, err := runner.Output(context.Background(), "spec", "--bundle="+dir)
	require.NoError(t, err)

	raw, err := os.ReadFile(filepath.Join(dir, "config.json"))
	require.NoError(t, err)
	t.Logf("runsc spec default config.json:\n%s", raw)
}
