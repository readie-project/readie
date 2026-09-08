// Package app wires the worker's components together and owns its lifecycle.
//
// This is the composition root: it is the only place that constructs concrete
// implementations, and every collaborator below it receives its dependencies
// through its constructor. Deps exists so tests can substitute the process
// boundaries - the listener, the container runtime and the router connection -
// without touching anything else.
package app

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/illinoisdata/readie/worker/internal/artifact"
	"github.com/illinoisdata/readie/worker/internal/clock"
	"github.com/illinoisdata/readie/worker/internal/config"
	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/grpcserver"
	"github.com/illinoisdata/readie/worker/internal/logging"
	"github.com/illinoisdata/readie/worker/internal/registry"
	"github.com/illinoisdata/readie/worker/internal/runsc"
	"github.com/illinoisdata/readie/worker/internal/sandbox"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// Deps are the process boundaries, injected so they can be replaced in tests.
type Deps struct {
	// Listen creates the gRPC listener.
	Listen func(network, addr string) (net.Listener, error)
	// NewRuntime builds the sandbox runtime. Substituting here - above the
	// runsc adapter - is what keeps the integration suite runnable on a
	// development machine, since gVisor is Linux-only.
	NewRuntime func(ctx context.Context, cfg config.Config, layout container.DirLayout, log *slog.Logger) (sandbox.Port, error)
	// LoadArtifacts discovers the generations this worker can run.
	LoadArtifacts func(cfg config.Config, log *slog.Logger) (container.Artifacts, error)
	// DialRegistry connects to the router, returning a client and the closer
	// for the underlying connection.
	DialRegistry func(ctx context.Context, target string) (pb.RegistryServiceClient, io.Closer, error)
	// Clock drives retry loops.
	Clock clock.Clock
}

func (d Deps) withDefaults() Deps {
	if d.Listen == nil {
		d.Listen = net.Listen
	}
	if d.NewRuntime == nil {
		d.NewRuntime = newRunscRuntime
	}
	if d.LoadArtifacts == nil {
		d.LoadArtifacts = loadArtifacts
	}
	if d.DialRegistry == nil {
		d.DialRegistry = dialRegistry
	}
	if d.Clock == nil {
		d.Clock = clock.NewSystem()
	}
	return d
}

// newRunscRuntime builds the gVisor adapter.
func newRunscRuntime(
	_ context.Context,
	cfg config.Config,
	layout container.DirLayout,
	log *slog.Logger,
) (sandbox.Port, error) {
	return runsc.New(runsc.Options{
		Binary:            cfg.RunscBinary,
		Root:              cfg.RunscRoot,
		BundlesDir:        layout.BundlesRoot(),
		Network:           cfg.SandboxNetwork,
		HostUDS:           cfg.SandboxHostUDS,
		Overlay:           cfg.SandboxOverlay,
		Platform:          cfg.SandboxPlatform,
		NVProxy:           cfg.SandboxGPU,
		IgnoreCgroups:     cfg.SandboxIgnoreCgroups,
		Debug:             cfg.SandboxDebug,
		DebugLogDir:       cfg.SandboxDebugLogDir,
		CommandTimeout:    cfg.RuntimeCommandTimeout,
		CheckpointTimeout: cfg.CheckpointTimeout,
		StatsInterval:     cfg.StatsInterval,
		Log:               log,
	})
}

// loadArtifacts reads the rootfs and checkpoints baked into this image.
//
// The root is a compiled-in constant rather than configuration: there is one
// place a worker image puts its artifacts, and making it settable invited a
// deployment where the mount and the expectation disagreed. Load still takes it
// as a parameter, so tests build fixtures in temporary directories.
func loadArtifacts(_ config.Config, log *slog.Logger) (container.Artifacts, error) {
	return artifact.Load(artifact.Options{Root: config.ArtifactRoot, Log: log})
}

func dialRegistry(_ context.Context, target string) (pb.RegistryServiceClient, io.Closer, error) {
	creds := insecure.NewCredentials()
	// grpc.NewClient is lazy: no connection is attempted until the first RPC,
	// which is why worker registration is where router reachability is proven.
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(creds))
	if err != nil {
		return nil, nil, fmt.Errorf("create router client: %w", err)
	}
	return pb.NewRegistryServiceClient(conn), conn, nil
}

// closer is one entry on the shutdown stack.
type closer struct {
	name string
	fn   func(context.Context) error
}

// App is a fully constructed worker.
type App struct {
	cfg      config.Config
	log      *slog.Logger
	server   *grpcserver.Server
	manager  *container.Manager
	runner   *execution.Runner
	reporter registry.Reporter

	// degraded is non-nil when this worker started without a usable
	// generation. It cannot run anything, so it registers ERROR rather than
	// READY: the router keeps it visible and probed, and never places work on
	// it. is_selectable on the router side requires READY.
	degraded error

	// shutdown is unwound in reverse, so each step runs only if the step that
	// established it succeeded.
	shutdown []closer
}

// checkpointStore is the slice of the artifact registry the compatibility check
// needs: the installed checkpoint ids, and a way to discard them all when they
// cannot be restored into this worker. *artifact.Registry satisfies it.
type checkpointStore interface {
	Checkpoints() []string
	DropCheckpoints()
}

// versioner reports a sandbox runtime's own version. The real *runsc.Adapter
// has it; a runtime that cannot report one simply skips that half of the check.
type versioner interface {
	Version(ctx context.Context) (string, error)
}

// verifyCheckpointCompat refuses baked checkpoints this worker could never
// restore. A checkpoint is bound to the exact sandbox spec and runsc build it
// was captured under; if either has drifted - a different overlay, GPU mode,
// resource limit, or runsc version - every restore silently degrades to a cold
// start while the worker keeps advertising the checkpoints. When strict (the
// default) it drops them so the worker serves cold-only and the mismatch is
// loud; otherwise it warns and honours the operator's choice to tolerate it.
func verifyCheckpointCompat(
	ctx context.Context,
	cfg config.Config,
	artifacts container.Artifacts,
	manager *container.Manager,
	runtime sandbox.Port,
	log *slog.Logger,
) {
	store, ok := artifacts.(checkpointStore)
	if !ok {
		return
	}
	installed := store.Checkpoints()
	if len(installed) == 0 {
		return // already cold-only; nothing a mismatch could invalidate
	}

	rootfs, err := artifacts.Rootfs()
	if err != nil {
		return // degraded: no rootfs means no restore can run regardless
	}

	spec, err := runsc.BuildSpec(manager.CanonicalSpec(rootfs))
	if err != nil {
		// We cannot compute our own fingerprint, so we cannot prove a mismatch.
		// Leaving the checkpoints in place preserves today's tolerant behaviour.
		log.Error("cannot fingerprint this worker's sandbox; leaving baked checkpoints in place",
			logging.KeyError, err)
		return
	}
	fingerprint := runsc.Fingerprint(spec, cfg.SandboxOverlay, cfg.SandboxNetwork, cfg.SandboxGPU)

	var version string
	if v, ok := runtime.(versioner); ok {
		if got, verr := v.Version(ctx); verr != nil {
			log.Warn("cannot read the sandbox runtime version; skipping the runsc half of the compatibility check",
				logging.KeyError, verr)
		} else {
			version = got
		}
	}

	reasons := checkpointCompatReasons(artifacts.Manifest(), fingerprint, version)
	if len(reasons) == 0 {
		return
	}
	applyCheckpointCompat(cfg.CheckpointStrictCompat, len(installed), reasons, store, log)
}

// checkpointCompatReasons lists why baked checkpoints cannot restore into a
// worker whose spec fingerprint is workerFingerprint and runsc version is
// workerVersion. Empty means compatible. Each half is skipped when the manifest
// did not record it or the worker could not compute it, so a missing value is
// never reported as a mismatch.
func checkpointCompatReasons(manifest artifact.Manifest, workerFingerprint, workerVersion string) []string {
	var reasons []string
	if manifest.SpecFingerprint != "" && workerFingerprint != "" &&
		manifest.SpecFingerprint != workerFingerprint {
		reasons = append(reasons, fmt.Sprintf(
			"captured under spec fingerprint %s but this worker builds %s",
			manifest.SpecFingerprint, workerFingerprint))
	}
	if manifest.RunscVersion != "" && workerVersion != "" &&
		manifest.RunscVersion != workerVersion {
		reasons = append(reasons, fmt.Sprintf(
			"captured under runsc %q but this worker runs %q",
			manifest.RunscVersion, workerVersion))
	}
	return reasons
}

// applyCheckpointCompat acts on an incompatibility: drop the checkpoints when
// strict, otherwise warn and keep them. Returns whether it dropped them.
func applyCheckpointCompat(strict bool, count int, reasons []string, store checkpointStore, log *slog.Logger) bool {
	joined := strings.Join(reasons, "; ")
	if strict {
		log.Error("baked checkpoints are incompatible with this worker; refusing them and serving cold starts only",
			"checkpoints", count, "reasons", joined)
		store.DropCheckpoints()
		return true
	}
	log.Warn("baked checkpoints look incompatible with this worker; restores will fall back to cold starts",
		"checkpoints", count, "reasons", joined)
	return false
}

// New builds the worker and everything it depends on.
//
// Ordering is deliberate: the container runtime and router client are
// established, orphans from a previous process are reclaimed, and only then is
// the listener opened. The previous implementation began serving before either
// client existed, so a request arriving in that window dereferenced a nil
// runtime client.
func New(ctx context.Context, cfg config.Config, log *slog.Logger, deps Deps) (*App, error) {
	deps = deps.withDefaults()

	if log == nil {
		log = slog.Default()
	}
	log = log.With(logging.KeyWorkerID, cfg.WorkerID)

	// The runtime daemonises a sandbox and a gofer per container, both of
	// which reparent to PID 1 when the create command exits. This process does
	// not reap orphans, so being PID 1 would accumulate two zombies per
	// execution until the pid limit is reached. Run under an init.
	if os.Getpid() == 1 {
		log.Warn("running as PID 1 with no init: orphaned sandbox processes will " +
			"never be reaped. Set init: true in compose, or run under tini.")
	}

	app := &App{cfg: cfg, log: log}

	// Anything already pushed onto the shutdown stack must be unwound if a
	// later construction step fails.
	fail := func(err error) (*App, error) {
		if cleanupErr := app.Shutdown(context.WithoutCancel(ctx)); cleanupErr != nil {
			log.Error("could not unwind after a failed startup", logging.KeyError, cleanupErr)
		}
		return nil, err
	}

	layout := container.NewDirLayout(cfg.WorkerDir)

	// 1. Artifacts. A worker with no generation still starts: it serves health,
	// registers itself, and reports that it cannot run anything - which is far
	// easier to diagnose than a container that exits before it logs. Executions
	// then fail fast, naming the missing artifact.
	artifacts, err := deps.LoadArtifacts(cfg, log)
	if err != nil {
		return nil, fmt.Errorf("load sandbox artifacts: %w", err)
	}
	if rootfs, rootfsErr := artifacts.Rootfs(); rootfsErr != nil {
		app.degraded = rootfsErr
		log.Error("starting without a root filesystem; no execution can succeed",
			"artifact_root", config.ArtifactRoot, logging.KeyError, rootfsErr)
	} else {
		log.Info("sandbox artifacts loaded", "rootfs", rootfs)
	}

	// 2. Sandbox runtime, probed before anything depends on it.
	runtimePort, err := deps.NewRuntime(ctx, cfg, layout, log)
	if err != nil {
		return nil, fmt.Errorf("build the sandbox runtime: %w", err)
	}
	app.push("sandbox runtime", func(context.Context) error { return runtimePort.Close() })

	probeCtx, cancelProbe := context.WithTimeout(ctx, cfg.RuntimeProbeTimeout)
	err = runtimePort.Probe(probeCtx)
	cancelProbe()
	if err != nil {
		return fail(fmt.Errorf("probe the sandbox runtime: %w", err))
	}

	// 3. Router client. Lazy, so this cannot fail for connectivity reasons.
	registryClient, registryConn, err := deps.DialRegistry(ctx, cfg.RouterURI)
	if err != nil {
		return fail(fmt.Errorf("connect to the router: %w", err))
	}
	app.push("router connection", func(context.Context) error { return registryConn.Close() })

	// 4. Object graph. Pure construction, no I/O.
	capacity := registry.Capacity{
		MemTotal:     cfg.MemTotal,
		MaxExecutors: cfg.MaxExecutors,
		Flavor:       cfg.WorkerFlavor,
		GPUTotal:     cfg.GPUTotal,
	}
	reporter := registry.NewGRPCReporter(
		registryClient, cfg.WorkerID, cfg.WorkerURI, capacity, cfg.StatusTimeout, log)
	app.reporter = reporter

	manager, err := container.NewManager(container.ManagerDeps{
		Runtime:   runtimePort,
		Reporter:  reporter,
		Layout:    layout,
		Names:     container.NewUUIDNamer(cfg.ContainerNamePrefix),
		FS:        container.NewOSFS(),
		Artifacts: artifacts,
		Spec:      container.SpecFromConfig(cfg),
		Log:       log,
	})
	if err != nil {
		return fail(fmt.Errorf("build container manager: %w", err))
	}
	app.manager = manager

	// A baked checkpoint restores only into the exact sandbox it was captured
	// under. If this worker's spec or runsc build has drifted, every restore
	// would silently fall back to a cold start while the worker still advertised
	// the checkpoints - so validate before serving.
	verifyCheckpointCompat(ctx, cfg, artifacts, manager, runtimePort, log)

	dialer := executor.NewUnixDialer(layout, executor.RetryPolicy{
		Total:          cfg.DialTotalTimeout,
		Interval:       cfg.DialRetryInterval,
		AttemptTimeout: cfg.DialAttemptTimeout,
	}, deps.Clock, log)

	runner := execution.NewRunner(manager, dialer, reporter, execution.RunnerConfig{
		WorkerID:           cfg.WorkerID,
		ExecutionTimeout:   cfg.ExecutionTimeout,
		ReleaseTimeout:     cfg.ReleaseTimeout,
		StatusTimeout:      cfg.StatusTimeout,
		StreamLogs:         cfg.StreamLogs,
		StreamStats:        cfg.StreamStats,
		StatsInterval:      cfg.StatsInterval,
		MemGrowthThreshold: cfg.MemGrowthThreshold,
		MemGrowthFactor:    cfg.MemGrowthFactor,
		WorkerMemTotal:     cfg.MemTotal,
		MemoryHeadroom:     0.9,
		SessionOptions: executor.SessionOptions{
			ChunkSize:        cfg.ChunkSize,
			WriteTimeout:     cfg.SocketWriteTimeout,
			FirstByteTimeout: cfg.FirstByteTimeout,
			IdleTimeout:      cfg.ResponseIdleTimeout,
		},
	}, log)
	app.runner = runner

	// 5. Reclaim orphans before serving. A process killed without warning
	// leaves containers and directories behind, and startup is the only
	// opportunity to reclaim them; the previous implementation swept only on
	// shutdown, so a crash leaked them permanently.
	cleanupCtx, cancelCleanup := context.WithTimeout(ctx, cfg.CleanupTimeout)
	if cleanupErr := manager.CleanupOrphans(cleanupCtx); cleanupErr != nil {
		// Not fatal: a worker that cannot reclaim old containers can still
		// serve new ones.
		log.Warn("could not reclaim orphaned containers at startup", logging.KeyError, cleanupErr)
	}
	cancelCleanup()

	// 6. Listener and services. Health starts NOT_SERVING.
	listener, err := deps.Listen("tcp", cfg.ListenAddr)
	if err != nil {
		return fail(fmt.Errorf("listen on %s: %w", cfg.ListenAddr, err))
	}

	app.server = grpcserver.NewServer(
		listener,
		grpcserver.NewExecutionService(runner, log),
		grpcserver.ServerOptions{Log: log, EnableReflection: true},
	)
	app.push("gRPC server", func(ctx context.Context) error { return app.server.GracefulStop(ctx) })

	return app, nil
}

// Addr reports the address the gRPC server is listening on.
func (a *App) Addr() net.Addr { return a.server.Addr() }

// Run serves until ctx is cancelled or the server fails, then shuts down.
func (a *App) Run(ctx context.Context) error {
	serveErr := make(chan error, 1)
	go func() { serveErr <- a.server.Serve() }()

	// Registering is what proves the router is reachable, since the client is
	// lazily connected.
	if err := a.register(ctx); err != nil {
		return errors.Join(err, a.Shutdown(context.WithoutCancel(ctx)))
	}

	// Only now is the worker genuinely able to serve. Health tracks the
	// process, so it goes SERVING even when degraded: NOT_SERVING would have
	// the router's prober evict this worker after three strikes, hiding the
	// very state the ERROR registration exists to make visible.
	a.server.SetServing(true)
	if a.degraded != nil {
		// Not "ready". It is listening and registered, and it cannot run
		// anything - saying otherwise two lines after reporting that would be
		// the kind of log that costs someone an afternoon.
		a.log.Warn("worker serving but unusable; every execution will be refused",
			"addr", a.cfg.WorkerURI, "router", a.cfg.RouterURI,
			logging.KeyError, a.degraded)
	} else {
		a.log.Info("worker ready", "addr", a.cfg.WorkerURI, "router", a.cfg.RouterURI)
	}

	// Started after registration, so the router has a record to attach the load
	// to. It stops with ctx and is never waited on: a report in flight during
	// shutdown is worth abandoning, not draining.
	utilizationDone := make(chan struct{})
	go func() {
		defer close(utilizationDone)
		a.utilizationLoop(ctx)
	}()

	var runErr error
	select {
	case <-ctx.Done():
		a.log.Info("shutdown signal received")
	case err := <-serveErr:
		runErr = err
		if err != nil {
			a.log.Error("gRPC server stopped unexpectedly", logging.KeyError, err)
		}
	}

	shutdownErr := a.Shutdown(context.WithoutCancel(ctx))
	<-utilizationDone
	return errors.Join(runErr, shutdownErr)
}

// utilizationLoop reports this worker's load until ctx is cancelled.
//
// Best-effort throughout: the router losing a load report degrades scheduling
// quality for one interval and must never disturb an execution. Before this
// existed, PostWorkerUtilization had no caller at all - the worker sent only
// per-executor utilization - so the router's notion of worker-level load was
// permanently zero and it could only schedule on in-flight count.
func (a *App) utilizationLoop(ctx context.Context) {
	interval := a.cfg.UtilizationInterval
	if interval <= 0 {
		// Validate rejects this, but a Config built directly - as tests and
		// embedders do - skips Validate, and time.NewTicker panics on it. A
		// load report is not worth taking the worker down for.
		interval = 10 * time.Second
	}

	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			count, reserved := a.manager.Load()
			err := a.reporter.WorkerUtilization(ctx, registry.Utilization{
				MemUsed:       reserved,
				MemTotal:      a.cfg.MemTotal,
				ExecutorCount: count,
				GPUMemTotal:   a.cfg.GPUTotal,
			})
			if err != nil && ctx.Err() == nil {
				a.log.Warn("could not report worker utilization", logging.KeyError, err)
			}
		}
	}
}

// registrationStatus is what this worker tells the router about itself.
//
// ERROR rather than READY when it holds no usable generation. Registering READY
// would have the router place work here that can only fail; not registering at
// all would make the worker invisible, so an operator would see "no workers"
// with no indication that one is running and why it is useless.
func (a *App) registrationStatus() pb.Status {
	if a.degraded != nil {
		return pb.Status_STATUS_ERROR
	}
	return pb.Status_STATUS_READY
}

// register announces the worker to the router, retrying a transient outage.
func (a *App) register(ctx context.Context) error {
	var lastErr error

	for attempt := 0; attempt <= a.cfg.RegisterRetries; attempt++ {
		if attempt > 0 {
			select {
			case <-time.After(time.Second):
			case <-ctx.Done():
				return fmt.Errorf("register worker: %w", ctx.Err())
			}
		}

		lastErr = a.reporter.WorkerStatus(ctx, a.registrationStatus())
		if lastErr == nil {
			a.log.Info("registered with the router", "attempt", attempt+1)
			return nil
		}
		a.log.Warn("could not register with the router; retrying",
			"attempt", attempt+1, logging.KeyError, lastErr)
	}

	return fmt.Errorf("register worker after %d attempts: %w", a.cfg.RegisterRetries+1, lastErr)
}

// Shutdown unwinds everything Run and New established, in reverse.
//
// Every step gets its own bounded context derived from ctx, no step aborts the
// others, and failures are accumulated rather than fatal. The previous
// implementation called log.Fatalf from inside a shutdown defer, so a router
// hiccup during shutdown killed the process before it could clean up its
// containers.
func (a *App) Shutdown(ctx context.Context) error {
	var errs []error

	// Stop advertising before draining, so the router routes new work
	// elsewhere while in-flight executions finish.
	if a.reporter != nil {
		removeCtx, cancel := context.WithTimeout(ctx, a.cfg.StatusTimeout)
		if err := a.reporter.WorkerStatus(removeCtx, pb.Status_STATUS_REMOVED); err != nil {
			a.log.Warn("could not deregister from the router", logging.KeyError, err)
		}
		cancel()
	}

	for i := len(a.shutdown) - 1; i >= 0; i-- {
		step := a.shutdown[i]

		stepCtx, cancel := context.WithTimeout(ctx, a.cfg.ShutdownTimeout)
		err := step.fn(stepCtx)
		cancel()

		if err != nil {
			a.log.Error("shutdown step failed", "step", step.name, logging.KeyError, err)
			errs = append(errs, fmt.Errorf("shutdown %s: %w", step.name, err))
		}

		// The server draining no longer guarantees every in-flight execution's
		// container is gone: release now finishes in the background rather
		// than before the RPC returns. Give stragglers up to ReleaseTimeout -
		// the bound already placed on any single release - to land, then let
		// CleanupOrphans reconcile whatever is still left. A race between the
		// two is a harmless double-cleanup: Destroy already tolerates
		// ErrNotFound/ErrConflict.
		if step.name == "gRPC server" && a.manager != nil {
			if a.runner != nil {
				waitCtx, cancelWait := context.WithTimeout(ctx, a.cfg.ReleaseTimeout)
				if err := a.runner.WaitPendingReleases(waitCtx); err != nil {
					a.log.Warn("container releases still pending after drain window; cleanup orphans will finish what's left",
						logging.KeyError, err)
				}
				cancelWait()
			}

			cleanupCtx, cancelCleanup := context.WithTimeout(ctx, a.cfg.CleanupTimeout)
			if err := a.manager.CleanupOrphans(cleanupCtx); err != nil {
				a.log.Error("could not reclaim containers during shutdown", logging.KeyError, err)
				errs = append(errs, fmt.Errorf("reclaim containers: %w", err))
			}
			cancelCleanup()
		}
	}
	a.shutdown = nil

	a.log.Info("worker shut down")
	return errors.Join(errs...)
}

func (a *App) push(name string, fn func(context.Context) error) {
	a.shutdown = append(a.shutdown, closer{name: name, fn: fn})
}
