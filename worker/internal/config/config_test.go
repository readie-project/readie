package config_test

import (
	"errors"
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
		"SERVICE_NAME":          "worker",
		"PORT":                  "50052",
		"WORKER_DIR":            "/shared",
		"ROUTER_URI":            "router:50051",
		"SITEPACKAGES_TXT_PATH": "/app/sitepackages_path.txt",
	}
}

func getenvFrom(env map[string]string) config.Getenv {
	return func(key string) string { return env[key] }
}

func readFileReturning(contents string) config.ReadFile {
	return func(string) ([]byte, error) { return []byte(contents), nil }
}

func TestLoad_Defaults(t *testing.T) {
	cfg, err := config.Load(getenvFrom(validEnv()), readFileReturning("/opt/conda/lib/python3.11/site-packages"))
	require.NoError(t, err)

	// Router contract: scheduler.py looks the worker up by this ID and dials
	// this URI verbatim. Both are asserted here so a regression is loud.
	assert.Equal(t, "worker-1", cfg.WorkerID)
	assert.Equal(t, "worker:50052", cfg.WorkerURI)

	// The bind address must be the port alone, not the advertised hostname.
	assert.Equal(t, ":50052", cfg.ListenAddr)

	assert.Equal(t, "router:50051", cfg.RouterURI)
	assert.Equal(t, "test-agent", cfg.ExecutorImage)
	assert.Equal(t, "exec_container-", cfg.ContainerNamePrefix)
	assert.Equal(t, int64(50000), cfg.CPUQuota)
	assert.Equal(t, int64(100), cfg.PidsLimit)
	assert.Equal(t, "none", cfg.NetworkMode)
	assert.Empty(t, cfg.ContainerRuntime)
	assert.Equal(t, 1024*1024, cfg.ChunkSize)
	assert.Equal(t, slog.LevelInfo, cfg.LogLevel)
	assert.Equal(t, "json", cfg.LogFormat)
}

// The executor sleeps 30s before binding its socket, so a dial budget at or
// below that can never succeed against a cold container.
func TestLoad_DialBudgetExceedsExecutorCheckpointSleep(t *testing.T) {
	cfg, err := config.Load(getenvFrom(validEnv()), readFileReturning("/site-packages"))
	require.NoError(t, err)

	assert.Greater(t, cfg.DialTotalTimeout, 30*time.Second,
		"dial budget must exceed the executor's 30s pre-bind sleep (scripts/executor/app.py)")
}

// A trailing newline survives into the Docker bind spec and the daemon rejects
// it with an opaque error, so the loader must trim it.
func TestLoad_TrimsSitePackagesWhitespace(t *testing.T) {
	cfg, err := config.Load(getenvFrom(validEnv()), readFileReturning("/opt/conda/lib/python3.11/site-packages\n"))
	require.NoError(t, err)
	assert.Equal(t, "/opt/conda/lib/python3.11/site-packages", cfg.SitePackagesPath)
}

func TestLoad_MissingRequiredEnv(t *testing.T) {
	for _, key := range []string{"SERVICE_NAME", "PORT", "WORKER_DIR", "ROUTER_URI", "SITEPACKAGES_TXT_PATH"} {
		t.Run(key, func(t *testing.T) {
			env := validEnv()
			delete(env, key)

			_, err := config.Load(getenvFrom(env), readFileReturning("/site-packages"))
			require.Error(t, err)
			assert.ErrorIs(t, err, config.ErrMissingEnv)
			assert.Contains(t, err.Error(), key)
		})
	}
}

func TestLoad_InvalidValues(t *testing.T) {
	tests := []struct {
		name      string
		env       map[string]string
		fileValue string
		readErr   error
	}{
		{name: "empty site-packages file", fileValue: "   \n"},
		{name: "unreadable site-packages file", readErr: errors.New("permission denied")},
		{name: "bad log level", env: map[string]string{"LOG_LEVEL": "verbose"}, fileValue: "/site-packages"},
		{name: "bad log format", env: map[string]string{"LOG_FORMAT": "xml"}, fileValue: "/site-packages"},
		{name: "bad duration", env: map[string]string{"EXECUTION_TIMEOUT": "soon"}, fileValue: "/site-packages"},
		{name: "bad bool", env: map[string]string{"STREAM_LOGS": "maybe"}, fileValue: "/site-packages"},
		{name: "site-packages with colon", fileValue: "/opt:packages"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			env := validEnv()
			for k, v := range tt.env {
				env[k] = v
			}
			readFile := readFileReturning(tt.fileValue)
			if tt.readErr != nil {
				readFile = func(string) ([]byte, error) { return nil, tt.readErr }
			}

			_, err := config.Load(getenvFrom(env), readFile)
			require.Error(t, err)
			assert.ErrorIs(t, err, config.ErrInvalidEnv)
		})
	}
}

func TestLoad_Overrides(t *testing.T) {
	env := validEnv()
	env["WORKER_ID"] = "worker-7"
	env["EXECUTOR_IMAGE"] = "custom-executor:v2"
	env["CONTAINER_RUNTIME"] = "runsc"
	env["EXECUTION_TIMEOUT"] = "90s"
	env["STATS_INTERVAL"] = "250ms"
	env["STREAM_STATS"] = "false"
	env["LOG_LEVEL"] = "debug"
	env["LOG_FORMAT"] = "text"

	cfg, err := config.Load(getenvFrom(env), readFileReturning("/site-packages"))
	require.NoError(t, err)

	assert.Equal(t, "worker-7", cfg.WorkerID)
	assert.Equal(t, "custom-executor:v2", cfg.ExecutorImage)
	assert.Equal(t, "runsc", cfg.ContainerRuntime)
	assert.Equal(t, 90*time.Second, cfg.ExecutionTimeout)
	assert.Equal(t, 250*time.Millisecond, cfg.StatsInterval)
	assert.False(t, cfg.StreamStats)
	assert.True(t, cfg.StreamLogs)
	assert.Equal(t, slog.LevelDebug, cfg.LogLevel)
	assert.Equal(t, "text", cfg.LogFormat)
}

func TestValidate_ReportsAllProblemsAtOnce(t *testing.T) {
	err := config.Config{LogFormat: "yaml", ChunkSize: -1}.Validate()
	require.Error(t, err)

	for _, want := range []string{"WorkerID", "RouterURI", "ChunkSize", "LogFormat"} {
		assert.Contains(t, err.Error(), want)
	}
}
