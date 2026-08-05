package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	specs "github.com/opencontainers/runtime-spec/specs-go"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/runsc"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// This is the test the whole helper exists for. The offline pipeline captures
// checkpoints against a sandbox it describes through these params; the worker
// describes the sandbox it restores into through container.Manager. If the two
// descriptions disagree in any way a checkpoint is sensitive to, restores fail
// opaquely minutes into a request — so agreement is asserted here, on the
// fingerprint, rather than left to review.
func TestFingerprint_MatchesWhatTheWorkerWouldBuild(t *testing.T) {
	const (
		rootfs     = "/opt/executor-rootfs"
		entrypoint = "/app/executor/app.py"
		pythonPath = "/lib/python3.12/dist-packages"
		socketDir  = "/shared/exec_container-abc"
		overlay    = "root:memory"
		network    = "none"
	)

	// What the pipeline's capture/spec.py sends.
	pipelineSpec, err := runsc.BuildSpec(toCreateSpec(params{
		ID:           "checkpoint-builder",
		RootfsPath:   rootfs,
		RootReadonly: false,
		Args:         []string{"python", "-u", entrypoint},
		Env:          []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=" + pythonPath},
		Cwd:          "/",
		Mounts: []struct {
			Source      string   `json:"source"`
			Destination string   `json:"destination"`
			Type        string   `json:"type"`
			Options     []string `json:"options"`
		}{{Source: socketDir, Destination: "/tmp", Type: "bind", Options: []string{"rbind", "rw"}}},
		CPUQuota:  50000,
		CPUPeriod: 100000,
		PidsLimit: 100,
	}))
	require.NoError(t, err)

	// What the worker builds for a request. Constructed from the same fields
	// container.Manager.createSpec uses.
	workerSpec, err := runsc.BuildSpec(sandbox.CreateSpec{
		ID:           "exec_container-abc",
		RootfsPath:   rootfs,
		RootReadonly: false,
		Args:         []string{"python", "-u", entrypoint},
		Env:          []string{"EXECUTOR_DIR=" + "/tmp", "PYTHONPATH=" + pythonPath},
		Cwd:          "/",
		Mounts: []sandbox.Mount{{
			Source: socketDir, Destination: "/tmp", Type: "bind", Options: []string{"rbind", "rw"},
		}},
		// Deliberately different from the pipeline's: the per-request
		// allocation and cgroup must not affect compatibility.
		MemoryBytes: 512 << 20,
		CPUQuota:    50000,
		CPUPeriod:   100000,
		PidsLimit:   100,
		CgroupsPath: "/crfs/exec_container-abc",
	})
	require.NoError(t, err)

	assert.Equal(t,
		runsc.Fingerprint(pipelineSpec, overlay, network, false),
		runsc.Fingerprint(workerSpec, overlay, network, false),
		"the pipeline and the worker must describe the same sandbox")
}

// The sandbox mount path the pipeline writes must be the one the worker mounts
// its per-container directory at, or the executor's socket lands somewhere the
// worker never looks.
func TestSandboxMountPathAgreesWithTheWorker(t *testing.T) {
	assert.Equal(t, "/tmp", container.SandboxExecutorDir(),
		"the pipeline hardcodes this same path as SANDBOX_EXECUTOR_DIR")
}

func TestRun_WritesAConfigTheRuntimeWouldAccept(t *testing.T) {
	dir := t.TempDir()
	paramsPath := filepath.Join(dir, "params.json")

	raw, err := json.Marshal(map[string]any{
		"id":          "checkpoint-builder",
		"bundle_dir":  dir,
		"rootfs_path": "/opt/executor-rootfs",
		"args":        []string{"python", "-u", "/app/executor/app.py"},
		"env":         []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/lib/python3.12/dist-packages"},
		"cwd":         "/",
		"mounts": []map[string]any{{
			"source": "/shared/x", "destination": "/tmp",
			"type": "bind", "options": []string{"rbind", "rw"},
		}},
		"pids_limit": 100,
	})
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(paramsPath, raw, 0o644))

	os.Args = []string{"ocispec", "-params", paramsPath}
	resetFlags()
	require.NoError(t, run())

	written, err := os.ReadFile(filepath.Join(dir, "config.json"))
	require.NoError(t, err)

	var spec specs.Spec
	require.NoError(t, json.Unmarshal(written, &spec))

	assert.Equal(t, []string{"python", "-u", "/app/executor/app.py"}, spec.Process.Args)
	assert.False(t, spec.Process.Terminal,
		"a pty cannot be re-supplied at restore and would break log capture")
	assert.Contains(t, spec.Process.Env, "EXECUTOR_DIR=/tmp")

	// The socket bind must be present, or the worker can never reach the
	// executor in a restored sandbox.
	last := spec.Mounts[len(spec.Mounts)-1]
	assert.Equal(t, "/tmp", last.Destination)
	assert.Equal(t, "/shared/x", last.Source)
}

func TestRun_RejectsAnUnusableSpec(t *testing.T) {
	dir := t.TempDir()
	paramsPath := filepath.Join(dir, "params.json")

	// A relative rootfs is the kind of mistake that would otherwise surface as
	// a runtime failure at checkpoint-build time.
	raw, err := json.Marshal(map[string]any{
		"id": "x", "bundle_dir": dir, "rootfs_path": "rootfs",
		"args": []string{"python"},
	})
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(paramsPath, raw, 0o644))

	os.Args = []string{"ocispec", "-params", paramsPath}
	resetFlags()

	err = run()
	require.Error(t, err)
	assert.ErrorIs(t, err, sandbox.ErrInvalidSpec)
}
