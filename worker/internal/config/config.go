// Package config loads and validates the worker's runtime configuration from the
// environment. It replaces the mutable package-level globals of the previous
// implementation: a Config is an immutable value, constructed once in main and
// injected into every collaborator.
package config

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// Sentinel errors returned by Load and Validate.
var (
	// ErrMissingEnv indicates a required environment variable was unset or empty.
	ErrMissingEnv = errors.New("missing required environment variable")
	// ErrInvalidEnv indicates an environment variable was set but unusable.
	ErrInvalidEnv = errors.New("invalid environment variable")
)

// Defaults applied when the corresponding environment variable is unset.
const (
	// DefaultWorkerID must match the identifier the router hardcodes in
	// router/scheduler.py:provision. Changing it breaks scheduling.
	DefaultWorkerID = "worker-1"

	// DefaultExecutorImage is the container image holding the Python executor.
	DefaultExecutorImage = "test-agent"

	// ContainerNamePrefix identifies containers this worker owns. Orphan cleanup
	// matches on it, so it must remain stable across restarts.
	ContainerNamePrefix = "exec_container-"

	// DefaultChunkSize is the read/write granularity for the executor socket and
	// for gRPC response payloads.
	DefaultChunkSize = 1024 * 1024

	// ExecutorSocketName is the unix socket the Python executor binds inside its
	// container. See scripts/executor/app.py:create_socket_connection.
	ExecutorSocketName = "executor.sock"

	// ExecutorMountPath is where the per-container host directory is mounted
	// inside the executor. The executor reads it from $EXECUTOR_DIR.
	ExecutorMountPath = "/tmp"

	// SitePackagesMountPath is where the host site-packages tree is mounted
	// read-only inside the executor, and the value of its $PYTHONPATH.
	SitePackagesMountPath = "/tmp/site_packages"

	// CheckpointDirName is the sub-directory of WorkerDir holding checkpoints.
	CheckpointDirName = "checkpoints"
)

// Config is the fully resolved, validated worker configuration.
type Config struct {
	// Identity.
	//
	// WorkerID and WorkerURI are part of the router contract: the router looks
	// the worker up by ID and dials the URI verbatim.
	WorkerID  string
	WorkerURI string

	// ListenAddr is the address the gRPC server binds. It is deliberately
	// distinct from WorkerURI: the previous implementation listened on
	// "worker:50052" (the advertised hostname) rather than ":50052".
	ListenAddr string

	// RouterURI is the router's gRPC endpoint (RegistryService).
	RouterURI string

	// Filesystem.
	WorkerDir        string // host root for per-container directories
	SitePackagesPath string // host site-packages tree, bind-mounted read-only

	// Container provisioning.
	ExecutorImage       string
	ContainerNamePrefix string
	CPUQuota            int64
	PidsLimit           int64
	ContainerRuntime    string // "" for the default runtime, "runsc" for gVisor
	NetworkMode         string
	// ContainerStopTimeout bounds a container's own shutdown. It is short
	// because reclamation runs during the worker's shutdown, inside whatever
	// grace period the supervisor allows before it sends SIGKILL.
	ContainerStopTimeout time.Duration

	// Executor socket.
	ChunkSize           int
	DialTotalTimeout    time.Duration
	DialRetryInterval   time.Duration
	DialAttemptTimeout  time.Duration
	SocketWriteTimeout  time.Duration
	FirstByteTimeout    time.Duration
	ResponseIdleTimeout time.Duration

	// Execution.
	ExecutionTimeout time.Duration
	ReleaseTimeout   time.Duration
	StatusTimeout    time.Duration
	StreamLogs       bool
	StreamStats      bool
	StatsInterval    time.Duration

	// Lifecycle.
	RegisterTimeout   time.Duration
	RegisterRetries   int
	CleanupTimeout    time.Duration
	ShutdownTimeout   time.Duration
	DockerPingTimeout time.Duration

	// Observability.
	LogLevel  slog.Level
	LogFormat string // "json" or "text"
}

// Getenv reads an environment variable. os.Getenv satisfies it.
type Getenv func(string) string

// ReadFile reads a file from disk. os.ReadFile satisfies it.
type ReadFile func(string) ([]byte, error)

// Load builds a Config from the environment. Both dependencies are injected so
// configuration can be tested without touching the process environment or disk.
func Load(getenv Getenv, readFile ReadFile) (Config, error) {
	if getenv == nil {
		getenv = os.Getenv
	}
	if readFile == nil {
		readFile = os.ReadFile
	}

	port, err := requireEnv(getenv, "PORT")
	if err != nil {
		return Config{}, err
	}
	serviceName, err := requireEnv(getenv, "SERVICE_NAME")
	if err != nil {
		return Config{}, err
	}
	workerDir, err := requireEnv(getenv, "WORKER_DIR")
	if err != nil {
		return Config{}, err
	}
	routerURI, err := requireEnv(getenv, "ROUTER_URI")
	if err != nil {
		return Config{}, err
	}

	sitePackages, err := loadSitePackagesPath(getenv, readFile)
	if err != nil {
		return Config{}, err
	}

	logLevel, err := parseLogLevel(getenv("LOG_LEVEL"))
	if err != nil {
		return Config{}, err
	}

	cfg := Config{
		WorkerID:   valueOr(getenv("WORKER_ID"), DefaultWorkerID),
		WorkerURI:  fmt.Sprintf("%s:%s", serviceName, port),
		ListenAddr: ":" + port,
		RouterURI:  routerURI,

		WorkerDir:        workerDir,
		SitePackagesPath: sitePackages,

		ExecutorImage:       valueOr(getenv("EXECUTOR_IMAGE"), DefaultExecutorImage),
		ContainerNamePrefix: ContainerNamePrefix,
		CPUQuota:            50000,
		PidsLimit:           100,
		// gVisor is installed in the image but not yet wired up; see the
		// Dockerfile and the project README. Opt in with CONTAINER_RUNTIME=runsc.
		ContainerRuntime:     getenv("CONTAINER_RUNTIME"),
		NetworkMode:          "none",
		ContainerStopTimeout: 2 * time.Second,

		ChunkSize: DefaultChunkSize,
		// The executor sleeps 30s awaiting a checkpoint before it binds its
		// socket (scripts/executor/app.py). A budget below that can never
		// succeed for a container started cold rather than restored.
		DialTotalTimeout:    60 * time.Second,
		DialRetryInterval:   100 * time.Millisecond,
		DialAttemptTimeout:  time.Second,
		SocketWriteTimeout:  30 * time.Second,
		FirstByteTimeout:    time.Hour,
		ResponseIdleTimeout: 30 * time.Second,

		ExecutionTimeout: time.Hour,
		ReleaseTimeout:   30 * time.Second,
		StatusTimeout:    5 * time.Second,
		StreamLogs:       true,
		StreamStats:      true,
		StatsInterval:    time.Second,

		RegisterTimeout:   5 * time.Second,
		RegisterRetries:   3,
		CleanupTimeout:    30 * time.Second,
		ShutdownTimeout:   30 * time.Second,
		DockerPingTimeout: 5 * time.Second,

		LogLevel:  logLevel,
		LogFormat: valueOr(strings.ToLower(getenv("LOG_FORMAT")), "json"),
	}

	if err := applyDurationOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	if err := applyBoolOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate reports whether the configuration is internally consistent. Load
// calls it; callers constructing a Config directly (tests) should too.
func (c Config) Validate() error {
	var errs []error

	for _, f := range []struct {
		name  string
		value string
	}{
		{"WorkerID", c.WorkerID},
		{"WorkerURI", c.WorkerURI},
		{"ListenAddr", c.ListenAddr},
		{"RouterURI", c.RouterURI},
		{"WorkerDir", c.WorkerDir},
		{"SitePackagesPath", c.SitePackagesPath},
		{"ExecutorImage", c.ExecutorImage},
		{"ContainerNamePrefix", c.ContainerNamePrefix},
	} {
		if f.value == "" {
			errs = append(errs, fmt.Errorf("%w: %s is empty", ErrInvalidEnv, f.name))
		}
	}

	// A path containing whitespace produces a malformed Docker bind spec
	// ("<path>\n:/tmp/site_packages:ro"), which the daemon rejects opaquely.
	if strings.ContainsAny(c.SitePackagesPath, " \t\r\n:") {
		errs = append(errs, fmt.Errorf(
			"%w: SitePackagesPath %q contains whitespace or a colon and cannot be used in a bind spec",
			ErrInvalidEnv, c.SitePackagesPath))
	}

	if c.ChunkSize <= 0 {
		errs = append(errs, fmt.Errorf("%w: ChunkSize must be positive", ErrInvalidEnv))
	}
	if c.DialTotalTimeout <= 0 {
		errs = append(errs, fmt.Errorf("%w: DialTotalTimeout must be positive", ErrInvalidEnv))
	}
	if c.DialRetryInterval <= 0 {
		errs = append(errs, fmt.Errorf("%w: DialRetryInterval must be positive", ErrInvalidEnv))
	}
	if c.ExecutionTimeout <= 0 {
		errs = append(errs, fmt.Errorf("%w: ExecutionTimeout must be positive", ErrInvalidEnv))
	}
	if c.LogFormat != "json" && c.LogFormat != "text" {
		errs = append(errs, fmt.Errorf("%w: LogFormat %q must be \"json\" or \"text\"", ErrInvalidEnv, c.LogFormat))
	}
	if c.RegisterRetries < 0 {
		errs = append(errs, fmt.Errorf("%w: RegisterRetries must not be negative", ErrInvalidEnv))
	}

	return errors.Join(errs...)
}

// loadSitePackagesPath resolves the host site-packages directory. The path is
// stored in a file (written at image build time by a `python -c print(...)`),
// so it arrives with a trailing newline that must be trimmed before it can be
// interpolated into a Docker bind specification.
func loadSitePackagesPath(getenv Getenv, readFile ReadFile) (string, error) {
	path, err := requireEnv(getenv, "SITEPACKAGES_TXT_PATH")
	if err != nil {
		return "", err
	}

	contents, err := readFile(path)
	if err != nil {
		return "", fmt.Errorf("%w: reading SITEPACKAGES_TXT_PATH %q: %w", ErrInvalidEnv, path, err)
	}

	sitePackages := strings.TrimSpace(string(contents))
	if sitePackages == "" {
		return "", fmt.Errorf("%w: site-packages path file %q is empty", ErrInvalidEnv, path)
	}
	return sitePackages, nil
}

func applyDurationOverrides(getenv Getenv, cfg *Config) error {
	overrides := map[string]*time.Duration{
		"EXECUTION_TIMEOUT":     &cfg.ExecutionTimeout,
		"DIAL_TOTAL_TIMEOUT":    &cfg.DialTotalTimeout,
		"RESPONSE_IDLE_TIMEOUT": &cfg.ResponseIdleTimeout,
		"SHUTDOWN_TIMEOUT":      &cfg.ShutdownTimeout,
		"CLEANUP_TIMEOUT":       &cfg.CleanupTimeout,
		"STATS_INTERVAL":        &cfg.StatsInterval,
	}
	for name, target := range overrides {
		raw := getenv(name)
		if raw == "" {
			continue
		}
		d, err := time.ParseDuration(raw)
		if err != nil {
			return fmt.Errorf("%w: %s=%q is not a duration: %w", ErrInvalidEnv, name, raw, err)
		}
		*target = d
	}
	return nil
}

func applyBoolOverrides(getenv Getenv, cfg *Config) error {
	overrides := map[string]*bool{
		"STREAM_LOGS":  &cfg.StreamLogs,
		"STREAM_STATS": &cfg.StreamStats,
	}
	for name, target := range overrides {
		raw := getenv(name)
		if raw == "" {
			continue
		}
		b, err := strconv.ParseBool(raw)
		if err != nil {
			return fmt.Errorf("%w: %s=%q is not a boolean: %w", ErrInvalidEnv, name, raw, err)
		}
		*target = b
	}
	return nil
}

func parseLogLevel(raw string) (slog.Level, error) {
	if raw == "" {
		return slog.LevelInfo, nil
	}
	var level slog.Level
	if err := level.UnmarshalText([]byte(raw)); err != nil {
		return 0, fmt.Errorf("%w: LOG_LEVEL=%q: %w", ErrInvalidEnv, raw, err)
	}
	return level, nil
}

func requireEnv(getenv Getenv, name string) (string, error) {
	value := strings.TrimSpace(getenv(name))
	if value == "" {
		return "", fmt.Errorf("%w: %s", ErrMissingEnv, name)
	}
	return value, nil
}

func valueOr(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
