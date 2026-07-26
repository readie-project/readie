package config_test

import (
	"log/slog"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
)

// validEnv mirrors the environment baked into worker/Dockerfile.
func validEnv() map[string]string {
	return map[string]string{
		"SERVICE_NAME":  "worker",
		"PORT":          "50052",
		"WORKER_DIR":    "/shared",
		"ROUTER_URI":    "router:50051",
		"ARTIFACT_ROOT": "/var/lib/crfs",
	}
}

func getenvFrom(env map[string]string) config.Getenv {
	return func(key string) string { return env[key] }
}

func load(t *testing.T, env map[string]string) (config.Config, error) {
	t.Helper()
	return config.Load(getenvFrom(env))
}

func TestLoad_Defaults(t *testing.T) {
	cfg, err := load(t, validEnv())
	require.NoError(t, err)

	// Router contract: scheduler.py looks the worker up by this ID and dials
	// this URI verbatim. Both are asserted here so a regression is loud.
	assert.Equal(t, "worker-1", cfg.WorkerID)
	assert.Equal(t, "worker:50052", cfg.WorkerURI)

	// The bind address must be the port alone, not the advertised hostname.
	assert.Equal(t, ":50052", cfg.ListenAddr)

	assert.Equal(t, "router:50051", cfg.RouterURI)
	assert.Equal(t, "/var/lib/crfs", cfg.ArtifactRoot)
	assert.Empty(t, cfg.ActiveGeneration, "empty selects the newest generation")

	assert.Equal(t, "/usr/local/bin/runsc", cfg.RunscBinary)
	assert.Equal(t, "/run/crfs-runsc", cfg.RunscRoot)
	assert.Equal(t, "none", cfg.SandboxNetwork, "executors must not reach the network")
	assert.Equal(t, "exec_container-", cfg.ContainerNamePrefix)
	assert.Equal(t, int64(50000), cfg.CPUQuota)
	assert.Equal(t, int64(100000), cfg.CPUPeriod)
	assert.Equal(t, int64(100), cfg.PidsLimit)
	assert.Equal(t, 1024*1024, cfg.ChunkSize)
	assert.True(t, cfg.CheckpointStrictCompat)
	assert.Equal(t, slog.LevelInfo, cfg.LogLevel)
	assert.Equal(t, "json", cfg.LogFormat)
}

// The executor binds its socket inside the sandbox and the worker dials it
// from the host, which the runtime forbids unless told otherwise.
func TestLoad_AllowsTheExecutorToBindAHostVisibleSocket(t *testing.T) {
	cfg, err := load(t, validEnv())
	require.NoError(t, err)
	assert.Equal(t, "create", cfg.SandboxHostUDS)
}

// The overlay must give each sandbox copy-on-write over the shared rootfs
// without covering the socket bind or writing into the shared tree.
func TestLoad_OverlayIsCopyOnWriteInMemory(t *testing.T) {
	cfg, err := load(t, validEnv())
	require.NoError(t, err)

	assert.Equal(t, "root:memory", cfg.SandboxOverlay)
	assert.False(t, cfg.SandboxRootReadonly, "an overlay needs a writable root")
}

// The executor sleeps 30s before binding its socket, so a dial budget at or
// below that can never succeed against a cold sandbox.
func TestLoad_DialBudgetExceedsExecutorCheckpointSleep(t *testing.T) {
	cfg, err := load(t, validEnv())
	require.NoError(t, err)

	assert.Greater(t, cfg.DialTotalTimeout, 30*time.Second,
		"the dial budget must exceed the executor's pre-bind sleep (scripts/executor/app.py)")
}

func TestLoad_MissingRequiredEnv(t *testing.T) {
	for _, key := range []string{"SERVICE_NAME", "PORT", "WORKER_DIR", "ROUTER_URI", "ARTIFACT_ROOT"} {
		t.Run(key, func(t *testing.T) {
			env := validEnv()
			delete(env, key)

			_, err := load(t, env)
			require.Error(t, err)
			assert.ErrorIs(t, err, config.ErrMissingEnv)
			assert.Contains(t, err.Error(), key)
		})
	}
}

func TestLoad_InvalidValues(t *testing.T) {
	tests := []struct {
		name string
		env  map[string]string
	}{
		{"bad log level", map[string]string{"LOG_LEVEL": "verbose"}},
		{"bad log format", map[string]string{"LOG_FORMAT": "xml"}},
		{"bad duration", map[string]string{"EXECUTION_TIMEOUT": "soon"}},
		{"bad bool", map[string]string{"STREAM_LOGS": "maybe"}},
		{"relative artifact root", map[string]string{"ARTIFACT_ROOT": "artifacts"}},
		{"relative runsc root", map[string]string{"RUNSC_ROOT": "state"}},
		{"unknown network", map[string]string{"SANDBOX_NETWORK": "bridge"}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnv()
			for k, v := range tt.env {
				env[k] = v
			}

			_, err := load(t, env)
			require.Error(t, err)
			assert.ErrorIs(t, err, config.ErrInvalidEnv)
		})
	}
}

// An overlay covering every mount keeps the executor's socket in the upper
// layer, where the worker cannot see it — the execution then fails at dial
// time looking exactly like a dead executor, which is the wrong diagnosis.
func TestLoad_RejectsAnOverlayThatWouldHideTheSocket(t *testing.T) {
	env := validEnv()
	env["SANDBOX_OVERLAY"] = "all:memory"

	_, err := load(t, env)
	require.Error(t, err)
	assert.ErrorIs(t, err, config.ErrInvalidEnv)
	assert.Contains(t, err.Error(), "executor socket")
}

// "self" puts the upper layer inside the rootfs directory, which every sandbox
// on this worker shares.
func TestLoad_RejectsAnOverlayThatWritesIntoTheSharedRootfs(t *testing.T) {
	env := validEnv()
	env["SANDBOX_OVERLAY"] = "root:self"

	_, err := load(t, env)
	require.Error(t, err)
	assert.ErrorIs(t, err, config.ErrInvalidEnv)
	assert.Contains(t, err.Error(), "shared rootfs")
}

func TestLoad_Overrides(t *testing.T) {
	env := validEnv()
	env["WORKER_ID"] = "worker-7"
	env["ACTIVE_GENERATION"] = "gen-2026-06"
	env["RUNSC_BINARY"] = "/opt/bin/runsc"
	env["SANDBOX_PLATFORM"] = "systrap"
	env["SANDBOX_IGNORE_CGROUPS"] = "true"
	env["SANDBOX_DEBUG"] = "true"
	env["CHECKPOINT_STRICT_COMPAT"] = "false"
	env["EXECUTION_TIMEOUT"] = "90s"
	env["RESTORE_TIMEOUT"] = "45s"
	env["CHECKPOINT_TIMEOUT"] = "10m"
	env["STATS_INTERVAL"] = "250ms"
	env["STREAM_STATS"] = "false"
	env["LOG_LEVEL"] = "debug"
	env["LOG_FORMAT"] = "text"

	cfg, err := load(t, env)
	require.NoError(t, err)

	assert.Equal(t, "worker-7", cfg.WorkerID)
	assert.Equal(t, "gen-2026-06", cfg.ActiveGeneration)
	assert.Equal(t, "/opt/bin/runsc", cfg.RunscBinary)
	assert.Equal(t, "systrap", cfg.SandboxPlatform)
	assert.True(t, cfg.SandboxIgnoreCgroups)
	assert.True(t, cfg.SandboxDebug)
	assert.False(t, cfg.CheckpointStrictCompat)
	assert.Equal(t, 90*time.Second, cfg.ExecutionTimeout)
	assert.Equal(t, 45*time.Second, cfg.RestoreTimeout)
	assert.Equal(t, 10*time.Minute, cfg.CheckpointTimeout)
	assert.Equal(t, 250*time.Millisecond, cfg.StatsInterval)
	assert.False(t, cfg.StreamStats)
	assert.True(t, cfg.StreamLogs)
	assert.Equal(t, slog.LevelDebug, cfg.LogLevel)
	assert.Equal(t, "text", cfg.LogFormat)
}

func TestValidate_ReportsAllProblemsAtOnce(t *testing.T) {
	err := config.Config{LogFormat: "yaml", ChunkSize: -1}.Validate()
	require.Error(t, err)

	for _, want := range []string{"WorkerID", "RouterURI", "ArtifactRoot", "ChunkSize", "LogFormat"} {
		assert.Contains(t, err.Error(), want)
	}
}
