// Package integration exercises the assembled worker end to end.
//
// Everything above the process boundaries is real: the gRPC server, the
// execution runner, the container manager, the executor socket protocol. Only
// the three boundaries app.Deps exposes are substituted — the listener becomes
// a bufconn, the container runtime becomes fakedocker, and the router becomes a
// fakeregistry server on a second bufconn.
//
// The composition that makes this work is fakedocker's OnCreate hook: when the
// manager "creates" a container, the harness starts a fake Python executor on
// exactly the socket path that container's layout resolves to, which is what
// the worker then dials.
package integration

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/test/bufconn"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/app"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/clock"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/docker"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakedocker"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeexecutor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

type harness struct {
	t *testing.T

	App    *app.App
	Exec   pb.ExecutionServiceClient
	Health healthpb.HealthClient

	Conn   *grpc.ClientConn
	Docker *fakedocker.Docker
	Router *fakeregistry.Server
	Layout container.Layout
	Config config.Config

	WorkerDir string

	// executors is guarded because OnCreate fires from whichever goroutine is
	// provisioning, and concurrent requests provision at the same time.
	mu        sync.Mutex
	executors []*fakeexecutor.Server

	runErr   chan error
	runOnce  sync.Once
	runValue error
	cancel   context.CancelFunc
}

// lastExecutor returns the most recently started fake executor.
func (h *harness) lastExecutor() *fakeexecutor.Server {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.executors) == 0 {
		return nil
	}
	return h.executors[len(h.executors)-1]
}

func (h *harness) addExecutor(s *fakeexecutor.Server) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.executors = append(h.executors, s)
}

type option func(*harnessOptions)

type harnessOptions struct {
	executor  fakeexecutor.Options
	tuneCfg   func(*config.Config)
	tuneFakes func(*fakedocker.Docker, *fakeregistry.Server)
	// startExecutors controls whether creating a container boots a fake
	// executor on its socket. Disabling it reproduces an executor that never
	// comes up.
	startExecutors bool
}

func withExecutor(opts fakeexecutor.Options) option {
	return func(o *harnessOptions) { o.executor = opts }
}

func withConfig(tune func(*config.Config)) option {
	return func(o *harnessOptions) { o.tuneCfg = tune }
}

func withFakes(tune func(*fakedocker.Docker, *fakeregistry.Server)) option {
	return func(o *harnessOptions) { o.tuneFakes = tune }
}

func withoutExecutors() option {
	return func(o *harnessOptions) { o.startExecutors = false }
}

// newHarness assembles a running worker and returns clients for it.
func newHarness(t *testing.T, opts ...option) *harness {
	t.Helper()

	options := &harnessOptions{startExecutors: true}
	options.executor.Reply = []byte("executor-result")
	for _, opt := range opts {
		opt(options)
	}

	// t.TempDir on darwin yields a path under /var/folders/… which, once a
	// UUID container name and "/executor.sock" are appended, exceeds the
	// 104-byte sun_path limit and makes bind fail with "invalid argument".
	workerDir := fakeexecutor.ShortTempDir(t)

	cfg := testConfig(workerDir)
	if options.tuneCfg != nil {
		options.tuneCfg(&cfg)
	}

	fakeDocker := fakedocker.New()
	router := fakeregistry.NewServer()
	if options.tuneFakes != nil {
		options.tuneFakes(fakeDocker, router)
	}

	layout := container.NewDirLayout(workerDir)

	h := &harness{
		t:         t,
		Docker:    fakeDocker,
		Router:    router,
		Layout:    layout,
		Config:    cfg,
		WorkerDir: workerDir,
		runErr:    make(chan error, 1),
	}

	// This is the join between the two fakes: a "created" container gets a
	// real unix socket with a fake executor behind it, at the path the worker
	// will dial.
	if options.startExecutors {
		fakeDocker.OnCreate = func(id string) {
			h.addExecutor(fakeexecutor.Start(t, layout.SocketPath(id), options.executor))
		}
	}

	workerLis := bufconn.Listen(1024 * 1024)
	routerClient := serveFakeRouter(t, router)

	ctx, cancel := context.WithCancel(context.Background())
	h.cancel = cancel

	worker, err := app.New(ctx, cfg, logging.Discard(), app.Deps{
		Listen: func(string, string) (net.Listener, error) { return workerLis, nil },
		NewDocker: func(context.Context, *slog.Logger) (docker.Port, error) {
			return fakeDocker, nil
		},
		DialRegistry: func(context.Context, string) (pb.RegistryServiceClient, io.Closer, error) {
			return routerClient, io.NopCloser(nil), nil
		},
		Clock: clock.NewSystem(),
	})
	require.NoError(t, err)
	h.App = worker

	go func() { h.runErr <- worker.Run(ctx) }()

	conn := dialBufconn(t, workerLis)
	h.Conn = conn
	h.Exec = pb.NewExecutionServiceClient(conn)
	h.Health = healthpb.NewHealthClient(conn)

	t.Cleanup(func() {
		if err := h.Shutdown(); err != nil {
			t.Errorf("worker shutdown failed: %v", err)
		}
	})

	h.waitUntilServing()
	return h
}

func testConfig(workerDir string) config.Config {
	return config.Config{
		WorkerID:            "worker-1",
		WorkerURI:           "worker:50052",
		ListenAddr:          ":50052",
		RouterURI:           "router:50051",
		WorkerDir:           workerDir,
		SitePackagesPath:    "/opt/conda/lib/python3.11/site-packages",
		ExecutorImage:       "test-agent",
		ContainerNamePrefix: config.ContainerNamePrefix,
		CPUQuota:            50000,
		PidsLimit:           100,
		NetworkMode:         "none",

		ChunkSize:           1024 * 1024,
		DialTotalTimeout:    5 * time.Second,
		DialRetryInterval:   10 * time.Millisecond,
		DialAttemptTimeout:  time.Second,
		SocketWriteTimeout:  5 * time.Second,
		FirstByteTimeout:    5 * time.Second,
		ResponseIdleTimeout: 300 * time.Millisecond,

		ExecutionTimeout: 10 * time.Second,
		ReleaseTimeout:   5 * time.Second,
		StatusTimeout:    2 * time.Second,
		StreamLogs:       false, // opt in per test; log timing is inherently racy
		StreamStats:      false,
		StatsInterval:    10 * time.Millisecond,

		RegisterTimeout:   2 * time.Second,
		RegisterRetries:   1,
		CleanupTimeout:    5 * time.Second,
		ShutdownTimeout:   10 * time.Second,
		DockerPingTimeout: 2 * time.Second,

		LogLevel:  slog.LevelError,
		LogFormat: "text",
	}
}

// serveFakeRouter runs the fake RegistryService on its own bufconn and returns
// a real gRPC client for it, so the worker exercises genuine RPC machinery.
func serveFakeRouter(t *testing.T, fake *fakeregistry.Server) pb.RegistryServiceClient {
	t.Helper()

	lis := bufconn.Listen(1024 * 1024)
	srv := grpc.NewServer()
	pb.RegisterRegistryServiceServer(srv, fake)

	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	return pb.NewRegistryServiceClient(dialBufconn(t, lis))
}

func dialBufconn(t *testing.T, lis *bufconn.Listener) *grpc.ClientConn {
	t.Helper()

	// The passthrough scheme is required: grpc.NewClient defaults to the DNS
	// resolver and would try to resolve "bufnet" as a hostname.
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	require.NoError(t, err)
	t.Cleanup(func() { _ = conn.Close() })

	return conn
}

// waitUntilServing blocks until the worker reports itself healthy.
func (h *harness) waitUntilServing() {
	h.t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	for {
		res, err := h.Health.Check(ctx, &healthpb.HealthCheckRequest{})
		if err == nil && res.GetStatus() == healthpb.HealthCheckResponse_SERVING {
			return
		}
		select {
		case <-ctx.Done():
			h.t.Fatalf("worker never reported SERVING: %v", err)
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// Shutdown cancels the worker and waits for Run to return. It is safe to call
// more than once: tests shut down explicitly and cleanup does so again.
func (h *harness) Shutdown() error {
	h.runOnce.Do(func() {
		h.cancel()
		select {
		case h.runValue = <-h.runErr:
		case <-time.After(30 * time.Second):
			h.runValue = errors.New("worker did not shut down within 30s")
		}
	})
	return h.runValue
}

// dialBufconnFor returns the harness's existing connection, for building
// additional clients (reflection, health) against the same worker.
func dialBufconnFor(t *testing.T, h *harness) *grpc.ClientConn {
	t.Helper()
	return h.Conn
}

// newHarnessExpectingFailure assembles a worker whose startup is expected to
// fail, and returns the error Run produced.
func newHarnessExpectingFailure(t *testing.T, tuneFakes func(*fakedocker.Docker, *fakeregistry.Server)) error {
	t.Helper()

	workerDir := fakeexecutor.ShortTempDir(t)
	cfg := testConfig(workerDir)
	cfg.RegisterRetries = 0

	fakeDocker := fakedocker.New()
	router := fakeregistry.NewServer()
	if tuneFakes != nil {
		tuneFakes(fakeDocker, router)
	}

	workerLis := bufconn.Listen(1024 * 1024)
	routerClient := serveFakeRouter(t, router)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	worker, err := app.New(ctx, cfg, logging.Discard(), app.Deps{
		Listen: func(string, string) (net.Listener, error) { return workerLis, nil },
		NewDocker: func(context.Context, *slog.Logger) (docker.Port, error) {
			return fakeDocker, nil
		},
		DialRegistry: func(context.Context, string) (pb.RegistryServiceClient, io.Closer, error) {
			return routerClient, io.NopCloser(nil), nil
		},
		Clock: clock.NewSystem(),
	})
	if err != nil {
		return err
	}
	return worker.Run(ctx)
}
