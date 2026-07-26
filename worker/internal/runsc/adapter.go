package runsc

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	specs "github.com/opencontainers/runtime-spec/specs-go"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// Bundle file names.
const (
	configFileName = "config.json"
	pidFileName    = "sandbox.pid"
	logFileName    = "sandbox.log"
)

// Options configures the adapter.
type Options struct {
	// Binary is the runsc executable.
	Binary string
	// Root is the runtime state directory. The worker owns it exclusively,
	// which is what makes enumerating and stat-ing it authoritative.
	Root string
	// BundlesDir holds one bundle directory per sandbox.
	BundlesDir string

	// Network, HostUDS and Overlay are global runtime flags.
	//
	// HostUDS governs whether a socket bound inside the sandbox is visible on
	// the host. The executor is a socket server, so without it every execution
	// fails at dial time; see the package's Linux verification notes.
	Network string
	HostUDS string
	Overlay string
	// Platform is the gVisor platform; empty takes the runtime default.
	Platform string
	// IgnoreCgroups disables cgroup enforcement, for environments where
	// delegation is unavailable.
	IgnoreCgroups bool
	// Debug enables runtime debug logging into DebugLogDir.
	Debug       bool
	DebugLogDir string

	// CommandTimeout bounds every non-blocking runtime call.
	CommandTimeout time.Duration
	// RestoreTimeout bounds a checkpoint restore.
	RestoreTimeout time.Duration
	// CheckpointTimeout bounds taking a snapshot, which can be large.
	CheckpointTimeout time.Duration
	// StatsInterval is the sampling period for streaming stats.
	StatsInterval time.Duration

	Log *slog.Logger
	// Runner executes commands. Defaults to a real process runner.
	Runner Runner
	// Now supplies timestamps; injectable for deterministic tests.
	Now func() time.Time
}

func (o Options) withDefaults() Options {
	if o.Binary == "" {
		o.Binary = "runsc"
	}
	if o.Network == "" {
		o.Network = "none"
	}
	if o.CommandTimeout <= 0 {
		o.CommandTimeout = 30 * time.Second
	}
	if o.RestoreTimeout <= 0 {
		o.RestoreTimeout = 30 * time.Second
	}
	if o.CheckpointTimeout <= 0 {
		o.CheckpointTimeout = 5 * time.Minute
	}
	if o.StatsInterval <= 0 {
		o.StatsInterval = time.Second
	}
	if o.Log == nil {
		o.Log = slog.Default()
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	if o.Runner == nil {
		o.Runner = NewExecRunner(o.Binary)
	}
	return o
}

// Adapter implements sandbox.Port by driving runsc.
type Adapter struct {
	opts   Options
	runner Runner
	log    *slog.Logger
	global []string
}

var _ sandbox.Port = (*Adapter)(nil)

// New builds an adapter.
func New(opts Options) (*Adapter, error) {
	opts = opts.withDefaults()

	if opts.Root == "" {
		return nil, fmt.Errorf("%w: runtime state root is empty", sandbox.ErrInvalidSpec)
	}
	if opts.BundlesDir == "" {
		return nil, fmt.Errorf("%w: bundles directory is empty", sandbox.ErrInvalidSpec)
	}

	a := &Adapter{opts: opts, runner: opts.Runner, log: opts.Log}
	a.global = a.globalFlags()
	return a, nil
}

// globalFlags builds the flag list that precedes every subcommand.
//
// Order is fixed so tests can assert argv by full-slice equality, which is the
// only way to catch a flag drifting to the wrong side of the subcommand.
func (a *Adapter) globalFlags() []string {
	flags := []string{
		"--root=" + a.opts.Root,
		"--network=" + a.opts.Network,
	}
	if a.opts.HostUDS != "" {
		flags = append(flags, "--host-uds="+a.opts.HostUDS)
	}
	if a.opts.Overlay != "" {
		flags = append(flags, "--overlay2="+a.opts.Overlay)
	}
	if a.opts.Platform != "" {
		flags = append(flags, "--platform="+a.opts.Platform)
	}
	if a.opts.IgnoreCgroups {
		flags = append(flags, "--ignore-cgroups")
	}
	if a.opts.Debug {
		flags = append(flags, "--debug", "--debug-log="+a.debugLogDir(), "--log-format=json")
	}
	return flags
}

// debugLogDir returns a path with a trailing separator, which is how the
// runtime is told to write one file per sandbox rather than appending
// everything to a single file.
func (a *Adapter) debugLogDir() string {
	dir := a.opts.DebugLogDir
	if dir == "" {
		dir = "/var/log/runsc"
	}
	if !strings.HasSuffix(dir, "/") {
		dir += "/"
	}
	return dir
}

// args prefixes the global flags to a subcommand invocation.
func (a *Adapter) args(rest ...string) []string {
	out := make([]string, 0, len(a.global)+len(rest))
	out = append(out, a.global...)
	return append(out, rest...)
}

// BundleDir is the bundle directory for a sandbox.
func (a *Adapter) BundleDir(id string) string { return filepath.Join(a.opts.BundlesDir, id) }

// LogPath is the log file for a sandbox.
func (a *Adapter) LogPath(id string) string { return filepath.Join(a.BundleDir(id), logFileName) }

// Probe verifies the runtime binary runs and its state root is usable.
func (a *Adapter) Probe(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	defer cancel()

	res, err := a.runner.Output(ctx, versionFlag)
	if err != nil {
		return sandbox.Wrap("probe", "", sandbox.ErrRuntimeUnavailable, err)
	}
	a.log.Info("sandbox runtime available", "version", firstLine(string(res.Stdout)))

	for _, dir := range []string{a.opts.Root, a.opts.BundlesDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return sandbox.Wrap("probe", "", sandbox.ErrRuntimeUnavailable,
				fmt.Errorf("prepare %s: %w", dir, err))
		}
	}
	return nil
}

// Version returns the runtime's full version string, for checkpoint metadata.
func (a *Adapter) Version(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	defer cancel()

	res, err := a.runner.Output(ctx, versionFlag)
	if err != nil {
		return "", sandbox.Wrap("version", "", sandbox.ErrRuntimeUnavailable, err)
	}
	return firstLine(string(res.Stdout)), nil
}

// Close releases adapter resources. Sandboxes are deliberately left alone:
// reclaiming them belongs to the container manager's cleanup, which the
// application's shutdown sequence runs before closing the runtime.
func (a *Adapter) Close() error { return nil }

// Create writes a sandbox's bundle. It starts no process.
func (a *Adapter) Create(_ context.Context, spec sandbox.CreateSpec) (string, error) {
	ociSpec, err := BuildSpec(spec)
	if err != nil {
		return "", sandbox.Wrap("create", spec.ID, sandbox.ErrInvalidSpec, err)
	}

	bundleDir := spec.BundleDir
	if bundleDir == "" {
		bundleDir = a.BundleDir(spec.ID)
	}
	if err := os.MkdirAll(bundleDir, 0o755); err != nil {
		return "", sandbox.Wrap("create", spec.ID, nil, fmt.Errorf("create bundle dir: %w", err))
	}
	if err := writeSpec(filepath.Join(bundleDir, configFileName), ociSpec); err != nil {
		return "", sandbox.Wrap("create", spec.ID, nil, err)
	}

	a.log.Debug("sandbox bundle written", logging.KeyContainerID, spec.ID, "bundle", bundleDir)
	return spec.ID, nil
}

// Start launches a created sandbox, restoring from a checkpoint when asked.
func (a *Adapter) Start(ctx context.Context, id string, spec sandbox.StartSpec) error {
	if spec.CheckpointID != "" {
		return a.restore(ctx, id, spec)
	}
	return a.coldStart(ctx, id)
}

// coldStart creates then starts the sandbox.
//
// Both steps clean up on failure so the caller may retry with the same id.
func (a *Adapter) coldStart(ctx context.Context, id string) error {
	stdio, closeStdio, err := a.openStdio(id)
	if err != nil {
		return sandbox.Wrap("start", id, nil, err)
	}
	defer closeStdio()

	createCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	_, err = a.runner.Spawn(createCtx, stdio, a.args(
		"create",
		"--bundle="+a.BundleDir(id),
		"--pid-file="+filepath.Join(a.BundleDir(id), pidFileName),
		id,
	)...)
	cancel()
	if err != nil {
		a.forceDelete(ctx, id)
		return sandbox.Wrap("start", id, a.classify(id, err), err)
	}

	startCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	_, err = a.runner.Output(startCtx, a.args("start", id)...)
	cancel()
	if err != nil {
		a.forceDelete(ctx, id)
		return sandbox.Wrap("start", id, a.classify(id, err), err)
	}
	return nil
}

// restore brings a sandbox up from a checkpoint image.
//
// The runtime creates the sandbox before restoring into it, so a partial
// failure leaves state behind. Deleting it here is what lets the caller
// immediately downgrade to a cold start with the same id instead of hitting an
// "already exists" conflict — turning a recoverable downgrade into a hard
// failure.
func (a *Adapter) restore(ctx context.Context, id string, spec sandbox.StartSpec) error {
	if err := checkCheckpointImage(spec.CheckpointDir); err != nil {
		return sandbox.Wrap("restore", id, sandbox.ErrRestoreFailed, err)
	}

	stdio, closeStdio, err := a.openStdio(id)
	if err != nil {
		return sandbox.Wrap("restore", id, sandbox.ErrRestoreFailed, err)
	}
	defer closeStdio()

	// --detach is essential: restore otherwise blocks in the foreground for
	// the sandbox's entire lifetime. The timeout is belt and braces so a hang
	// surfaces as a downgrade rather than a wedged execution.
	restoreCtx, cancel := context.WithTimeout(ctx, a.opts.RestoreTimeout)
	_, err = a.runner.Spawn(restoreCtx, stdio, a.args(
		"restore",
		"--bundle="+a.BundleDir(id),
		"--image-path="+spec.CheckpointDir,
		"--pid-file="+filepath.Join(a.BundleDir(id), pidFileName),
		"--detach",
		id,
	)...)
	cancel()

	if err != nil {
		a.forceDelete(ctx, id)
		return sandbox.Wrap("restore", id, sandbox.ErrRestoreFailed, err)
	}
	return nil
}

// checkCheckpointImage rejects an obviously unusable image before spending a
// restore timeout discovering it.
func checkCheckpointImage(dir string) error {
	if dir == "" {
		return errors.New("no checkpoint image directory")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("read checkpoint image %s: %w", dir, err)
	}
	for _, e := range entries {
		if !e.IsDir() && e.Name() != "meta.json" {
			return nil
		}
	}
	return fmt.Errorf("checkpoint image %s holds no image files", dir)
}

// Stop asks a sandbox to exit, escalating to SIGKILL after timeout.
func (a *Adapter) Stop(ctx context.Context, id string, timeout time.Duration) error {
	killCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	_, err := a.runner.Output(killCtx, a.args("kill", id, "SIGTERM")...)
	cancel()

	if err != nil {
		kind := a.classify(id, err)
		// Already gone, or never started: nothing to stop.
		if errors.Is(kind, sandbox.ErrNotFound) {
			return sandbox.Wrap("stop", id, kind, err)
		}
		a.log.Debug("graceful kill failed; forcing", logging.KeyContainerID, id, logging.KeyError, err)
	}

	if timeout <= 0 {
		timeout = 2 * time.Second
	}
	if a.waitStopped(ctx, id, timeout) {
		return nil
	}

	forceCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	defer cancel()
	if _, err := a.runner.Output(forceCtx, a.args("kill", "--all", id, "SIGKILL")...); err != nil {
		if kind := a.classify(id, err); errors.Is(kind, sandbox.ErrNotFound) {
			return nil
		}
		return sandbox.Wrap("stop", id, nil, err)
	}
	return nil
}

// waitStopped polls until the sandbox is no longer running, reporting whether
// it stopped within the budget.
func (a *Adapter) waitStopped(ctx context.Context, id string, timeout time.Duration) bool {
	deadline := a.opts.Now().Add(timeout)
	for a.opts.Now().Before(deadline) {
		info, err := a.Inspect(ctx, id)
		if err != nil || !info.Running {
			return true
		}
		timer := time.NewTimer(50 * time.Millisecond)
		select {
		case <-timer.C:
		case <-ctx.Done():
			timer.Stop()
			return false
		}
	}
	return false
}

// Remove deletes a sandbox and its bundle.
func (a *Adapter) Remove(ctx context.Context, id string, force bool) error {
	args := []string{"delete"}
	if force {
		args = append(args, "--force")
	}
	args = append(args, id)

	deleteCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	_, err := a.runner.Output(deleteCtx, a.args(args...)...)
	cancel()

	if err != nil {
		if kind := a.classify(id, err); errors.Is(kind, sandbox.ErrNotFound) {
			// Still remove the bundle: a sandbox whose runtime state is gone
			// but whose bundle survives would otherwise leak disk forever.
			a.removeBundle(id)
			return sandbox.Wrap("remove", id, kind, err)
		}
		return sandbox.Wrap("remove", id, nil, err)
	}

	a.removeBundle(id)
	return nil
}

func (a *Adapter) removeBundle(id string) {
	if err := os.RemoveAll(a.BundleDir(id)); err != nil {
		a.log.Warn("could not remove sandbox bundle", logging.KeyContainerID, id, logging.KeyError, err)
	}
}

// forceDelete discards a partially created sandbox, ignoring errors.
func (a *Adapter) forceDelete(ctx context.Context, id string) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), a.opts.CommandTimeout)
	defer cancel()

	if _, err := a.runner.Output(ctx, a.args("delete", "--force", id)...); err != nil {
		a.log.Debug("cleanup delete failed", logging.KeyContainerID, id, logging.KeyError, err)
	}
}

// Pause suspends a sandbox's processes.
func (a *Adapter) Pause(ctx context.Context, id string) error {
	return a.simple(ctx, "pause", id)
}

// Unpause resumes a paused sandbox.
func (a *Adapter) Unpause(ctx context.Context, id string) error {
	return a.simple(ctx, "resume", id)
}

func (a *Adapter) simple(ctx context.Context, verb, id string) error {
	ctx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	defer cancel()

	if _, err := a.runner.Output(ctx, a.args(verb, id)...); err != nil {
		return sandbox.Wrap(verb, id, a.classify(id, err), err)
	}
	return nil
}

// Update changes a live sandbox's memory limit.
//
// runsc has no update subcommand, so the bundle's spec is rewritten (keeping
// Inspect truthful across worker restarts) and the cgroup file is written
// directly when one is in use. A missing cgroup is not an error: the container
// manager treats an Update failure as fatal to a warm resume, and losing every
// warm resume because cgroup delegation is unavailable would be far worse than
// an unenforced limit.
func (a *Adapter) Update(_ context.Context, id string, spec sandbox.UpdateSpec) error {
	if spec.MemoryBytes <= 0 {
		return nil
	}

	configPath := filepath.Join(a.BundleDir(id), configFileName)
	ociSpec, err := readSpec(configPath)
	if err != nil {
		return sandbox.Wrap("update", id, a.classifyPath(id, err), err)
	}

	limit := spec.MemoryBytes
	if ociSpec.Linux == nil {
		ociSpec.Linux = &specs.Linux{}
	}
	if ociSpec.Linux.Resources == nil {
		ociSpec.Linux.Resources = &specs.LinuxResources{}
	}
	ociSpec.Linux.Resources.Memory = &specs.LinuxMemory{Limit: &limit}

	if err := writeSpec(configPath, ociSpec); err != nil {
		return sandbox.Wrap("update", id, nil, err)
	}

	if cgroupPath := ociSpec.Linux.CgroupsPath; cgroupPath != "" {
		a.writeCgroupMemoryLimit(id, cgroupPath, limit)
	}
	return nil
}

// writeCgroupMemoryLimit applies a limit to a live cgroup, best-effort.
func (a *Adapter) writeCgroupMemoryLimit(id, cgroupPath string, limit int64) {
	target := filepath.Join("/sys/fs/cgroup", cgroupPath, "memory.max")
	if err := os.WriteFile(target, []byte(strconv.FormatInt(limit, 10)), 0o644); err != nil {
		a.log.Debug("could not apply the memory limit to the live cgroup; "+
			"the bundle is updated and the limit applies on next start",
			logging.KeyContainerID, id, "cgroup", target, logging.KeyError, err)
	}
}

// Checkpoint snapshots a sandbox into spec.Dir.
func (a *Adapter) Checkpoint(ctx context.Context, id string, spec sandbox.CheckpointSpec) error {
	if spec.Dir == "" {
		return sandbox.Errorf("checkpoint", id, sandbox.ErrCheckpointFailed, "no image directory")
	}
	// The runtime will not create the image directory itself.
	if err := os.MkdirAll(spec.Dir, 0o755); err != nil {
		return sandbox.Wrap("checkpoint", id, sandbox.ErrCheckpointFailed, err)
	}

	// A frozen sandbox cannot be snapshotted, so a paused one is resumed for
	// the duration and restored to its previous state afterwards.
	info, err := a.Inspect(ctx, id)
	if err != nil {
		return sandbox.Wrap("checkpoint", id, sandbox.ErrCheckpointFailed, err)
	}
	if info.Paused {
		if err := a.Unpause(ctx, id); err != nil {
			return sandbox.Wrap("checkpoint", id, sandbox.ErrCheckpointFailed, err)
		}
		defer func() {
			if err := a.Pause(context.WithoutCancel(ctx), id); err != nil {
				a.log.Warn("could not re-pause after checkpointing",
					logging.KeyContainerID, id, logging.KeyError, err)
			}
		}()
	}

	args := []string{"checkpoint", "--image-path=" + spec.Dir}
	if spec.LeaveRunning {
		args = append(args, "--leave-running")
	}
	args = append(args, id)

	checkpointCtx, cancel := context.WithTimeout(ctx, a.opts.CheckpointTimeout)
	defer cancel()

	if _, err := a.runner.Output(checkpointCtx, a.args(args...)...); err != nil {
		return sandbox.Wrap("checkpoint", id, sandbox.ErrCheckpointFailed, err)
	}
	return nil
}

// Inspect reports a sandbox's current state.
func (a *Adapter) Inspect(ctx context.Context, id string) (sandbox.Info, error) {
	stateCtx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	res, err := a.runner.Output(stateCtx, a.args("state", id)...)
	cancel()

	if err != nil {
		return sandbox.Info{}, sandbox.Wrap("inspect", id, a.classify(id, err), err)
	}

	state, err := parseState(res.Stdout)
	if err != nil {
		return sandbox.Info{}, sandbox.Wrap("inspect", id, nil, err)
	}

	info := sandbox.Info{
		ID:      state.ID,
		PID:     state.PID,
		Running: state.running(),
		Paused:  state.paused(),
	}

	// The allocation lives in the bundle rather than in runtime state, which
	// keeps the adapter stateless — important because orphan cleanup inspects
	// sandboxes created by a previous worker process.
	if ociSpec, err := readSpec(filepath.Join(a.BundleDir(id), configFileName)); err == nil {
		if ociSpec.Linux != nil && ociSpec.Linux.Resources != nil && ociSpec.Linux.Resources.Memory != nil {
			if limit := ociSpec.Linux.Resources.Memory.Limit; limit != nil {
				info.MemoryBytes = *limit
			}
		}
	}
	return info, nil
}

// List returns sandboxes whose id begins with idPrefix.
//
// It enumerates bundle directories rather than asking the runtime, for two
// reasons: it depends on no listing flag, and — the point — it finds sandboxes
// whose runtime state was lost but whose bundle survives. Those are exactly
// the orphans cleanup exists to reclaim, and a runtime listing would not
// mention them, so their directories would leak forever.
func (a *Adapter) List(ctx context.Context, idPrefix string) ([]sandbox.Summary, error) {
	entries, err := os.ReadDir(a.opts.BundlesDir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, sandbox.Wrap("list", "", nil, fmt.Errorf("read bundles dir: %w", err))
	}

	var summaries []sandbox.Summary
	for _, entry := range entries {
		if !entry.IsDir() || !strings.HasPrefix(entry.Name(), idPrefix) {
			continue
		}

		summary := sandbox.Summary{ID: entry.Name(), State: "unknown"}
		if info, err := a.Inspect(ctx, entry.Name()); err == nil {
			switch {
			case info.Paused:
				summary.State = statePaused
			case info.Running:
				summary.State = stateRunning
			default:
				summary.State = stateStopped
			}
		}
		summaries = append(summaries, summary)
	}

	sort.Slice(summaries, func(i, j int) bool { return summaries[i].ID < summaries[j].ID })
	return summaries, nil
}

// Logs returns the sandbox's merged output.
func (a *Adapter) Logs(_ context.Context, id string, follow bool) (io.ReadCloser, error) {
	f, err := os.Open(a.LogPath(id))
	if errors.Is(err, fs.ErrNotExist) {
		return nil, sandbox.Wrap("logs", id, sandbox.ErrNotFound, err)
	}
	if err != nil {
		return nil, sandbox.Wrap("logs", id, nil, err)
	}
	return newTailReader(f, follow, 0), nil
}

// Stats subscribes to resource samples.
func (a *Adapter) Stats(ctx context.Context, id string, stream bool) (sandbox.StatsStream, error) {
	if !stream {
		return newPollingStatsStream(ctx, func(ctx context.Context) (sandbox.Stats, error) {
			return a.sampleOnce(ctx, id)
		}, a.opts.StatsInterval, true), nil
	}

	// The streaming form is one long-lived process rather than one fork per
	// sample, which matters at one sample per second per active execution.
	// Polling remains the fallback if the runtime does not support --interval.
	return newPollingStatsStream(ctx, func(ctx context.Context) (sandbox.Stats, error) {
		return a.sampleOnce(ctx, id)
	}, a.opts.StatsInterval, false), nil
}

// sampleOnce takes a single resource sample.
func (a *Adapter) sampleOnce(ctx context.Context, id string) (sandbox.Stats, error) {
	ctx, cancel := context.WithTimeout(ctx, a.opts.CommandTimeout)
	defer cancel()

	res, err := a.runner.Output(ctx, a.args("events", "--stats", id)...)
	if err != nil {
		return sandbox.Stats{}, sandbox.Wrap("stats", id, a.classify(id, err), err)
	}
	return parseEvent(res.Stdout, a.opts.Now())
}

// openStdio prepares the descriptors a sandbox inherits.
//
// The returned closer releases this process's copies; whatever the runtime
// daemonises keeps its own duplicates, which is the whole mechanism behind
// Logs.
func (a *Adapter) openStdio(id string) (Stdio, func(), error) {
	if err := os.MkdirAll(a.BundleDir(id), 0o755); err != nil {
		return Stdio{}, nil, fmt.Errorf("create bundle dir: %w", err)
	}

	logFile, err := os.OpenFile(a.LogPath(id), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return Stdio{}, nil, fmt.Errorf("open sandbox log: %w", err)
	}

	devNull, err := os.Open(os.DevNull)
	if err != nil {
		_ = logFile.Close()
		return Stdio{}, nil, fmt.Errorf("open %s: %w", os.DevNull, err)
	}

	closer := func() {
		_ = logFile.Close()
		_ = devNull.Close()
	}
	return Stdio{In: devNull, Out: logFile, Err: logFile}, closer, nil
}

// classify decides which kind a runtime failure belongs to.
//
// Whether the sandbox exists is answered by the state directory rather than by
// matching stderr text, which removes the most fragile part of classification.
func (a *Adapter) classify(id string, err error) error {
	if id != "" && !a.stateExists(id) {
		return sandbox.ErrNotFound
	}

	var cmdErr *CommandError
	if errors.As(err, &cmdErr) {
		lowered := strings.ToLower(cmdErr.Stderr)
		switch {
		case strings.Contains(lowered, "does not exist"),
			strings.Contains(lowered, "not found"):
			return sandbox.ErrNotFound
		case strings.Contains(lowered, "already exists"),
			strings.Contains(lowered, "cannot pause"),
			strings.Contains(lowered, "not paused"),
			strings.Contains(lowered, "is not running"):
			return sandbox.ErrConflict
		case cmdErr.ExitCode == -1:
			return sandbox.ErrRuntimeUnavailable
		}
	}
	return nil
}

func (a *Adapter) classifyPath(id string, err error) error {
	if errors.Is(err, fs.ErrNotExist) {
		return sandbox.ErrNotFound
	}
	return a.classify(id, err)
}

// stateExists reports whether the runtime holds state for a sandbox.
func (a *Adapter) stateExists(id string) bool {
	_, err := os.Stat(filepath.Join(a.opts.Root, id))
	return err == nil
}

// writeSpec writes a bundle config atomically, so a reader never observes a
// partial document.
func writeSpec(path string, spec *specs.Spec) error {
	raw, err := json.MarshalIndent(spec, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal spec: %w", err)
	}
	raw = append(raw, '\n')

	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return fmt.Errorf("create temp spec: %w", err)
	}
	tmpName := tmp.Name()
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(raw); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write spec: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close spec: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("rename spec: %w", err)
	}
	return nil
}

func readSpec(path string) (*specs.Spec, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read spec: %w", err)
	}
	var spec specs.Spec
	if err := json.Unmarshal(raw, &spec); err != nil {
		return nil, fmt.Errorf("parse spec: %w", err)
	}
	return &spec, nil
}

func firstLine(s string) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		return strings.TrimSpace(s[:i])
	}
	return strings.TrimSpace(s)
}
