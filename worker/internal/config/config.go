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
	"path/filepath"
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
	// DefaultWorkerID is used when WORKER_ID is unset. It is only a default:
	// the router indexes workers by whatever id they register with, so a fleet
	// must give each worker its own. (An earlier router hardcoded "worker-1"
	// in provision(); it no longer does, and that constraint is gone.)
	DefaultWorkerID = "worker-1"

	// ContainerNamePrefix identifies containers this worker owns. Orphan cleanup
	// matches on it, so it must remain stable across restarts.
	ContainerNamePrefix = "exec_container-"

	// DefaultChunkSize is the read/write granularity for the executor socket and
	// for gRPC response payloads.
	DefaultChunkSize = 1024 * 1024

	// ExecutorSocketName is the unix socket the Python executor binds inside its
	// container. See executor/src/readie_executor/server.py.
	ExecutorSocketName = "executor.sock"

	// ExecutorMountPath is where the per-container host directory is mounted
	// inside the sandbox. The executor reads it from $EXECUTOR_DIR and binds
	// its socket there, which is how the worker reaches it.
	ExecutorMountPath = "/tmp"

	// ArtifactRoot is where the worker image bakes its root filesystem and
	// checkpoints. Compiled in rather than configurable: there is one place a
	// worker image puts them, and a settable path invited a deployment where
	// the mount and the expectation disagreed. The image build and this
	// constant are the only two things that need to agree, and both live in
	// this repository.
	ArtifactRoot = "/var/lib/readie"

	// BundlesDirName is the sub-directory of WorkerDir holding OCI bundles.
	// Bundles sit outside the per-container directories on purpose: those are
	// mounted into the sandbox, and a sandbox has no business reading its own
	// runtime spec.
	BundlesDirName = "sandboxes"

	// SandboxLogName is the per-sandbox log file inside its bundle directory.
	// The runtime has no log API, so this file is the only source of output.
	SandboxLogName = "sandbox.log"
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
	//
	// WorkerDir is the host root for per-container directories and bundles. It
	// is writable runtime scratch and stays a volume; the artifacts do not,
	// they are baked into the image at ArtifactRoot.
	WorkerDir string

	// Sandbox runtime.
	RunscBinary string
	RunscRoot   string // runtime state directory, owned exclusively by this worker
	// SandboxNetwork is "none": executors must not reach the network.
	SandboxNetwork string
	// SandboxHostUDS governs whether a socket bound inside the sandbox is
	// visible on the host. The executor is a socket server, so anything that
	// forbids it makes every execution fail at dial time.
	SandboxHostUDS string
	// SandboxOverlay gives each sandbox copy-on-write over the shared, read-only
	// rootfs. It must never cover the socket bind mount.
	SandboxOverlay string
	// SandboxRootReadonly is coupled to the overlay mode and must match what a
	// checkpoint was captured under.
	SandboxRootReadonly bool
	SandboxPlatform     string
	// SandboxIgnoreCgroups disables cgroup enforcement where delegation is
	// unavailable, such as inside some nested-container environments.
	SandboxIgnoreCgroups bool
	SandboxDebug         bool
	SandboxDebugLogDir   string

	// Container provisioning.
	ContainerNamePrefix string
	CPUQuota            int64
	CPUPeriod           int64
	PidsLimit           int64
	CgroupParent        string
	// DefaultContainerMem is the memory limit applied when a request carries no
	// memory budget. Without it a budgetless request runs unbounded.
	DefaultContainerMem int64
	// MemGrowthThreshold is the fraction of a container's memory limit whose use
	// triggers a proactive expansion; MemGrowthFactor is how much the limit is
	// multiplied by when it does. Auto-expand stops at the request's max budget,
	// or the worker's own capacity when the request sets none.
	MemGrowthThreshold float64
	MemGrowthFactor    float64
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

	// Runtime call bounds.
	RuntimeCommandTimeout time.Duration
	RestoreTimeout        time.Duration
	CheckpointTimeout     time.Duration
	// CheckpointStrictCompat controls what happens when, at startup, this
	// worker's spec fingerprint or runsc version disagrees with the manifest the
	// baked checkpoints were captured under. True (the default) drops the
	// checkpoints so the worker serves cold starts only and logs the mismatch
	// loudly; false keeps them and only warns, tolerating restores that fall back
	// to cold starts. See verifyCheckpointCompat in internal/app.
	CheckpointStrictCompat bool

	// WorkerFlavor is "cpu" (CPU-only) or "gpu" (CPU + GPU). It is advertised to
	// the router, which routes GPU requests only to gpu workers and prefers cpu
	// workers for CPU requests.
	WorkerFlavor string
	// SandboxGPU enables gVisor's nvproxy and the NVIDIA env in the sandbox
	// spec. It defaults to true on a gpu worker and can be overridden with
	// SANDBOX_GPU; it must match what a checkpoint was captured under, so it is
	// folded into the spec fingerprint.
	SandboxGPU bool

	// Capacity, reported to the router so it can schedule on real limits
	// rather than in-flight count alone.
	//
	// MemTotal is the byte budget this worker will hand out to executors. It
	// is configured rather than read from the host because the worker is
	// itself usually containerised, where /proc/meminfo describes the host and
	// not the cgroup the worker actually lives in.
	MemTotal int64
	// GPUTotal is the GPU device memory, in bytes, this worker offers. Like
	// MemTotal it is configured, not measured; zero on a cpu worker.
	GPUTotal int64
	// MaxExecutors caps concurrent containers. Zero means unbounded.
	MaxExecutors int32
	// UtilizationInterval paces worker-level load reports.
	UtilizationInterval time.Duration

	// Execution.
	ExecutionTimeout time.Duration
	ReleaseTimeout   time.Duration
	StatusTimeout    time.Duration
	StreamLogs       bool
	StreamStats      bool
	StatsInterval    time.Duration

	// Lifecycle.
	RegisterTimeout     time.Duration
	RegisterRetries     int
	CleanupTimeout      time.Duration
	ShutdownTimeout     time.Duration
	RuntimeProbeTimeout time.Duration

	// Observability.
	LogLevel  slog.Level
	LogFormat string // "json" or "text"
}

// Getenv reads an environment variable. os.Getenv satisfies it.
type Getenv func(string) string

// Load builds a Config from the environment. getenv is injected so
// configuration can be tested without touching the process environment.
func Load(getenv Getenv) (Config, error) {
	if getenv == nil {
		getenv = os.Getenv
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

	logLevel, err := parseLogLevel(getenv("LOG_LEVEL"))
	if err != nil {
		return Config{}, err
	}

	cfg := Config{
		WorkerID:   valueOr(getenv("WORKER_ID"), DefaultWorkerID),
		WorkerURI:  fmt.Sprintf("%s:%s", serviceName, port),
		ListenAddr: ":" + port,
		RouterURI:  routerURI,

		WorkerDir: workerDir,

		RunscBinary:    valueOr(getenv("RUNSC_BINARY"), "/usr/local/bin/runsc"),
		RunscRoot:      valueOr(getenv("RUNSC_ROOT"), "/run/readie-runsc"),
		SandboxNetwork: valueOr(getenv("SANDBOX_NETWORK"), "none"),
		// The executor binds its socket inside the sandbox and the worker dials
		// it from the host, which the runtime forbids by default.
		SandboxHostUDS: valueOr(getenv("SANDBOX_HOST_UDS"), "create"),
		// Copy-on-write over the shared rootfs, held in sandbox memory. Paired
		// with a writable root; the two must move together or a restore fails.
		SandboxOverlay:      valueOr(getenv("SANDBOX_OVERLAY"), "root:memory"),
		SandboxRootReadonly: false,
		SandboxPlatform:     getenv("SANDBOX_PLATFORM"),
		SandboxDebugLogDir:  valueOr(getenv("SANDBOX_DEBUG_LOG_DIR"), "/var/log/runsc"),

		WorkerFlavor: valueOr(getenv("WORKER_FLAVOR"), "cpu"),

		ContainerNamePrefix:  ContainerNamePrefix,
		CPUQuota:             50000,
		CPUPeriod:            100000,
		PidsLimit:            100,
		CgroupParent:         valueOr(getenv("CGROUP_PARENT"), "/readie"),
		ContainerStopTimeout: 2 * time.Second,
		DefaultContainerMem:  512 << 20,
		MemGrowthThreshold:   0.9,
		MemGrowthFactor:      2.0,

		RuntimeCommandTimeout:  30 * time.Second,
		RestoreTimeout:         30 * time.Second,
		CheckpointTimeout:      5 * time.Minute,
		CheckpointStrictCompat: true,

		ChunkSize: DefaultChunkSize,

		DialTotalTimeout:    60 * time.Second,
		DialRetryInterval:   100 * time.Millisecond,
		DialAttemptTimeout:  time.Second,
		SocketWriteTimeout:  30 * time.Second,
		FirstByteTimeout:    time.Hour,
		ResponseIdleTimeout: 30 * time.Second,

		// 4 GiB is a deliberately conservative default: over-reporting capacity
		// makes the router overcommit a worker, and the failure that produces
		// is an OOM-killed sandbox mid-execution.
		MemTotal:            4 << 30,
		MaxExecutors:        0,
		UtilizationInterval: 10 * time.Second,

		ExecutionTimeout: time.Hour,
		ReleaseTimeout:   30 * time.Second,
		StatusTimeout:    5 * time.Second,
		StreamLogs:       true,
		StreamStats:      true,
		StatsInterval:    time.Second,

		RegisterTimeout:     5 * time.Second,
		RegisterRetries:     3,
		CleanupTimeout:      30 * time.Second,
		ShutdownTimeout:     30 * time.Second,
		RuntimeProbeTimeout: 5 * time.Second,

		LogLevel:  logLevel,
		LogFormat: valueOr(strings.ToLower(getenv("LOG_FORMAT")), "json"),
	}

	if err := applyDurationOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	if err := applyIntOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	if err := applyFloatOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	if err := applyBoolOverrides(getenv, &cfg); err != nil {
		return Config{}, err
	}
	// A gpu worker runs the GPU sandbox by default; SANDBOX_GPU (handled above)
	// overrides when set.
	if strings.TrimSpace(getenv("SANDBOX_GPU")) == "" {
		cfg.SandboxGPU = cfg.WorkerFlavor == "gpu"
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
		{"RunscBinary", c.RunscBinary},
		{"RunscRoot", c.RunscRoot},
		{"SandboxNetwork", c.SandboxNetwork},
		{"ContainerNamePrefix", c.ContainerNamePrefix},
	} {
		if f.value == "" {
			errs = append(errs, fmt.Errorf("%w: %s is empty", ErrInvalidEnv, f.name))
		}
	}

	for _, f := range []struct {
		name  string
		value string
	}{
		{"WorkerDir", c.WorkerDir},
		{"RunscBinary", c.RunscBinary},
		{"RunscRoot", c.RunscRoot},
	} {
		if f.value != "" && !filepath.IsAbs(f.value) {
			errs = append(errs, fmt.Errorf("%w: %s %q must be an absolute path",
				ErrInvalidEnv, f.name, f.value))
		}
	}

	// An overlay covering every mount would keep the executor's socket in the
	// overlay's upper layer, where the worker cannot see it - every execution
	// would then fail at dial time looking exactly like a dead executor.
	if strings.HasPrefix(c.SandboxOverlay, "all:") {
		errs = append(errs, fmt.Errorf(
			"%w: SandboxOverlay %q would hide the executor socket from the worker; use a root: overlay",
			ErrInvalidEnv, c.SandboxOverlay))
	}
	// "self" places the upper layer inside the rootfs directory, which is
	// shared by every sandbox on this worker.
	if strings.Contains(c.SandboxOverlay, ":self") {
		errs = append(errs, fmt.Errorf(
			"%w: SandboxOverlay %q writes into the shared rootfs; use root:memory or root:dir=…",
			ErrInvalidEnv, c.SandboxOverlay))
	}
	switch c.SandboxNetwork {
	case "", "none", "sandbox", "host":
	default:
		errs = append(errs, fmt.Errorf("%w: SandboxNetwork %q must be none, sandbox or host",
			ErrInvalidEnv, c.SandboxNetwork))
	}
	switch c.WorkerFlavor {
	case "cpu", "gpu":
	default:
		errs = append(errs, fmt.Errorf("%w: WorkerFlavor %q must be cpu or gpu",
			ErrInvalidEnv, c.WorkerFlavor))
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
	if c.MemTotal < 0 {
		errs = append(errs, fmt.Errorf("%w: MemTotal must not be negative", ErrInvalidEnv))
	}
	if c.DefaultContainerMem <= 0 {
		errs = append(errs, fmt.Errorf("%w: DefaultContainerMem must be positive", ErrInvalidEnv))
	}
	if c.MemGrowthFactor <= 1 {
		errs = append(errs, fmt.Errorf("%w: MemGrowthFactor must exceed 1", ErrInvalidEnv))
	}
	if c.MemGrowthThreshold <= 0 || c.MemGrowthThreshold > 1 {
		errs = append(errs, fmt.Errorf("%w: MemGrowthThreshold must be in (0, 1]", ErrInvalidEnv))
	}
	if c.MaxExecutors < 0 {
		errs = append(errs, fmt.Errorf("%w: MaxExecutors must not be negative", ErrInvalidEnv))
	}
	if c.UtilizationInterval <= 0 {
		errs = append(errs, fmt.Errorf("%w: UtilizationInterval must be positive", ErrInvalidEnv))
	}
	if c.RegisterRetries < 0 {
		errs = append(errs, fmt.Errorf("%w: RegisterRetries must not be negative", ErrInvalidEnv))
	}

	return errors.Join(errs...)
}

func applyDurationOverrides(getenv Getenv, cfg *Config) error {
	overrides := map[string]*time.Duration{
		"EXECUTION_TIMEOUT":           &cfg.ExecutionTimeout,
		"DIAL_TOTAL_TIMEOUT":          &cfg.DialTotalTimeout,
		"RESPONSE_IDLE_TIMEOUT":       &cfg.ResponseIdleTimeout,
		"SHUTDOWN_TIMEOUT":            &cfg.ShutdownTimeout,
		"CLEANUP_TIMEOUT":             &cfg.CleanupTimeout,
		"STATS_INTERVAL":              &cfg.StatsInterval,
		"CONTAINER_STOP_TIMEOUT":      &cfg.ContainerStopTimeout,
		"RUNSC_COMMAND_TIMEOUT":       &cfg.RuntimeCommandTimeout,
		"RESTORE_TIMEOUT":             &cfg.RestoreTimeout,
		"CHECKPOINT_TIMEOUT":          &cfg.CheckpointTimeout,
		"WORKER_UTILIZATION_INTERVAL": &cfg.UtilizationInterval,
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

// applyIntOverrides reads the numeric capacity settings. WORKER_MEM_TOTAL
// accepts a plain byte count or a size suffix ("8Gi", "512Mi"), because a byte
// count for a memory budget is unreadable in a compose file.
func applyIntOverrides(getenv Getenv, cfg *Config) error {
	if raw := strings.TrimSpace(getenv("WORKER_MEM_TOTAL")); raw != "" {
		bytes, err := parseBytes(raw)
		if err != nil {
			return fmt.Errorf("%w: WORKER_MEM_TOTAL=%q: %w", ErrInvalidEnv, raw, err)
		}
		cfg.MemTotal = bytes
	}
	if raw := strings.TrimSpace(getenv("WORKER_MAX_EXECUTORS")); raw != "" {
		n, err := strconv.ParseInt(raw, 10, 32)
		if err != nil {
			return fmt.Errorf("%w: WORKER_MAX_EXECUTORS=%q is not an integer: %w", ErrInvalidEnv, raw, err)
		}
		cfg.MaxExecutors = int32(n)
	}
	if raw := strings.TrimSpace(getenv("DEFAULT_CONTAINER_MEM")); raw != "" {
		bytes, err := parseBytes(raw)
		if err != nil {
			return fmt.Errorf("%w: DEFAULT_CONTAINER_MEM=%q: %w", ErrInvalidEnv, raw, err)
		}
		cfg.DefaultContainerMem = bytes
	}
	if raw := strings.TrimSpace(getenv("WORKER_GPU_TOTAL")); raw != "" {
		bytes, err := parseBytes(raw)
		if err != nil {
			return fmt.Errorf("%w: WORKER_GPU_TOTAL=%q: %w", ErrInvalidEnv, raw, err)
		}
		cfg.GPUTotal = bytes
	}
	return nil
}

func applyFloatOverrides(getenv Getenv, cfg *Config) error {
	overrides := map[string]*float64{
		"MEM_GROWTH_THRESHOLD": &cfg.MemGrowthThreshold,
		"MEM_GROWTH_FACTOR":    &cfg.MemGrowthFactor,
	}
	for name, target := range overrides {
		raw := strings.TrimSpace(getenv(name))
		if raw == "" {
			continue
		}
		f, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			return fmt.Errorf("%w: %s=%q is not a number: %w", ErrInvalidEnv, name, raw, err)
		}
		*target = f
	}
	return nil
}

// byteSuffixes are checked longest-first so "Mi" is not read as "M".
var byteSuffixes = []struct {
	suffix string
	scale  int64
}{
	{"KiB", 1 << 10}, {"MiB", 1 << 20}, {"GiB", 1 << 30}, {"TiB", 1 << 40},
	{"Ki", 1 << 10}, {"Mi", 1 << 20}, {"Gi", 1 << 30}, {"Ti", 1 << 40},
	{"KB", 1e3}, {"MB", 1e6}, {"GB", 1e9}, {"TB", 1e12},
	{"K", 1e3}, {"M", 1e6}, {"G", 1e9}, {"T", 1e12},
}

func parseBytes(raw string) (int64, error) {
	for _, s := range byteSuffixes {
		if rest, ok := strings.CutSuffix(raw, s.suffix); ok {
			n, err := strconv.ParseInt(strings.TrimSpace(rest), 10, 64)
			if err != nil {
				return 0, err
			}
			return n * s.scale, nil
		}
	}
	return strconv.ParseInt(raw, 10, 64)
}

func applyBoolOverrides(getenv Getenv, cfg *Config) error {
	overrides := map[string]*bool{
		"STREAM_LOGS":              &cfg.StreamLogs,
		"STREAM_STATS":             &cfg.StreamStats,
		"SANDBOX_IGNORE_CGROUPS":   &cfg.SandboxIgnoreCgroups,
		"SANDBOX_DEBUG":            &cfg.SandboxDebug,
		"SANDBOX_GPU":              &cfg.SandboxGPU,
		"CHECKPOINT_STRICT_COMPAT": &cfg.CheckpointStrictCompat,
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
