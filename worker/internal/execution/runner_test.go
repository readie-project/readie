package execution_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/execution"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakesink"
)

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

// fakeContainers is a ContainerService recording lifecycle calls.
type fakeContainers struct {
	mu       sync.Mutex
	acquired []container.AcquireRequest
	released []container.Outcome

	handle     container.Handle
	acquireErr error
	logsErr    error
	statsErr   error

	logData     []byte
	logsHang    bool
	statsFrames []sandbox.Stats
}

func newFakeContainers() *fakeContainers {
	return &fakeContainers{handle: container.Handle{
		ID:      "exec_container-test",
		Alloc:   container.Allocation{CPUAlloc: 512 << 20},
		Created: true,
	}}
}

func (f *fakeContainers) Acquire(_ context.Context, req container.AcquireRequest) (container.Handle, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.acquired = append(f.acquired, req)
	if f.acquireErr != nil {
		return container.Handle{}, f.acquireErr
	}
	return f.handle, nil
}

func (f *fakeContainers) Release(_ context.Context, _ registry.ExecutionRef, _ container.Handle, outcome container.Outcome) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.released = append(f.released, outcome)
	return nil
}

func (f *fakeContainers) Logs(ctx context.Context, _ string) (io.ReadCloser, error) {
	if f.logsErr != nil {
		return nil, f.logsErr
	}
	if f.logsHang {
		return newHangingReader(ctx), nil
	}
	return io.NopCloser(bytes.NewReader(f.logData)), nil
}

func (f *fakeContainers) Stats(_ context.Context, _ string) (sandbox.StatsStream, error) {
	if f.statsErr != nil {
		return nil, f.statsErr
	}
	return &sliceStats{frames: f.statsFrames, closed: make(chan struct{})}, nil
}

func (f *fakeContainers) Outcomes() []container.Outcome {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]container.Outcome(nil), f.released...)
}

func (f *fakeContainers) AcquireRequests() []container.AcquireRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]container.AcquireRequest(nil), f.acquired...)
}

type hangingReader struct {
	ctx  context.Context
	once sync.Once
	done chan struct{}
}

func newHangingReader(ctx context.Context) *hangingReader {
	return &hangingReader{ctx: ctx, done: make(chan struct{})}
}

func (h *hangingReader) Read([]byte) (int, error) {
	select {
	case <-h.done:
	case <-h.ctx.Done():
	}
	return 0, io.EOF
}

func (h *hangingReader) Close() error {
	h.once.Do(func() { close(h.done) })
	return nil
}

type sliceStats struct {
	mu     sync.Mutex
	frames []sandbox.Stats
	i      int
	once   sync.Once
	closed chan struct{}
}

func (s *sliceStats) Recv() (sandbox.Stats, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.i >= len(s.frames) {
		return sandbox.Stats{}, io.EOF
	}
	frame := s.frames[s.i]
	s.i++
	return frame, nil
}

func (s *sliceStats) Close() error {
	s.once.Do(func() { close(s.closed) })
	return nil
}

// pipeDialer connects the runner to an in-process fake executor over net.Pipe,
// so the runner's orchestration can be tested without the filesystem.
type pipeDialer struct {
	reply       []byte
	replyDelay  time.Duration
	holdOpen    bool
	dialErr     error
	stop        chan struct{}
	mu          sync.Mutex
	requestBody []byte
}

func (d *pipeDialer) Dial(_ context.Context, _ string) (executor.Conn, error) {
	if d.dialErr != nil {
		return nil, d.dialErr
	}

	client, server := net.Pipe()
	go d.serve(server)
	return client, nil
}

func (d *pipeDialer) serve(conn executor.Conn) {
	defer func() { _ = conn.Close() }()

	var body []byte
	buf := make([]byte, 64*1024)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			chunk := buf[:n]
			if bytes.HasSuffix(chunk, executor.EOFMarker) {
				body = append(body, bytes.TrimSuffix(chunk, executor.EOFMarker)...)
				break
			}
			body = append(body, chunk...)
		}
		if err != nil {
			return
		}
	}

	d.mu.Lock()
	d.requestBody = body
	d.mu.Unlock()

	if d.replyDelay > 0 {
		select {
		case <-time.After(d.replyDelay):
		case <-d.stop:
			return
		}
	}
	if _, err := conn.Write(d.reply); err != nil {
		return
	}
	if d.holdOpen {
		// Stay open so the reader must fall back to its idle timeout, but
		// unblock when the client hangs up rather than parking forever.
		_, _ = io.Copy(io.Discard, conn)
	}
}

func (d *pipeDialer) RequestBody() []byte {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]byte(nil), d.requestBody...)
}

// chunkSource yields a fixed set of request chunks.
type chunkSource struct {
	chunks [][]byte
	i      int
	err    error
}

func (c *chunkSource) Next(context.Context) ([]byte, error) {
	if c.err != nil {
		return nil, c.err
	}
	if c.i >= len(c.chunks) {
		return nil, io.EOF
	}
	chunk := c.chunks[c.i]
	c.i++
	return chunk, nil
}

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

type harness struct {
	runner     *execution.Runner
	containers *fakeContainers
	dialer     *pipeDialer
	registry   *fakeregistry.Recorder
	sink       *fakesink.Sink
}

func newHarness(t *testing.T, cfg execution.RunnerConfig, tune func(*harness)) *harness {
	t.Helper()

	h := &harness{
		containers: newFakeContainers(),
		dialer:     &pipeDialer{reply: []byte("result"), stop: make(chan struct{})},
		registry:   fakeregistry.NewRecorder(),
		sink:       fakesink.New(),
	}
	if tune != nil {
		tune(h)
	}
	t.Cleanup(func() { close(h.dialer.stop) })

	if cfg.WorkerID == "" {
		cfg.WorkerID = "worker-1"
	}
	if cfg.SessionOptions.FirstByteTimeout == 0 {
		cfg.SessionOptions.FirstByteTimeout = 5 * time.Second
	}
	if cfg.SessionOptions.IdleTimeout == 0 {
		cfg.SessionOptions.IdleTimeout = 200 * time.Millisecond
	}

	h.runner = execution.NewRunner(h.containers, h.dialer, h.registry, cfg, logging.Discard())
	return h
}

func request() execution.Request {
	return execution.Request{
		Ref:            registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"},
		Alloc:          container.Allocation{CPUAlloc: 512 << 20},
		InitialPayload: []byte("first-"),
	}
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

func TestRun_HappyPath(t *testing.T) {
	testutil.AssertNoLeak(t)
	h := newHarness(t, execution.RunnerConfig{}, nil)

	src := &chunkSource{chunks: [][]byte{[]byte("second-"), []byte("third")}}
	result, err := h.runner.Run(context.Background(), request(), src, h.sink)
	require.NoError(t, err)

	assert.Equal(t, "exec_container-test", result.Handle.ID)
	assert.Equal(t, "first-second-third", string(h.dialer.RequestBody()),
		"every chunk must reach the executor in order")
	assert.Equal(t, "result", string(h.sink.PayloadBytes()))
	assert.Equal(t, []container.Outcome{container.OutcomeSuccess}, h.containers.Outcomes())
}

// The router reads the scheduling decision off the first response it sees, so
// the sink has to be told before any output is produced.
func TestRun_ProvisionsBeforeAnyOutput(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, nil)

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	order := h.sink.CallOrder()
	require.NotEmpty(t, order)
	assert.Equal(t, "Provision", order[0])

	provisions := h.sink.Provisions()
	require.Len(t, provisions, 1, "Provision must be called exactly once")
	assert.Equal(t, "worker-1", provisions[0].WorkerID)
	assert.Equal(t, "exec_container-test", provisions[0].ContainerID)
	assert.Equal(t, int64(512<<20), provisions[0].CPUAlloc)
	// The router copies these straight through to its own client; the previous
	// implementation left them empty.
	assert.Equal(t, "req-1", provisions[0].Ref.RequestID)
	assert.Equal(t, "sess-1", provisions[0].Ref.SessionID)
}

// The regression test for the concurrent-Send bug: payload, logs and stats all
// produce at once, and the sink must still see strictly serial calls.
func TestRun_SinkIsNeverUsedConcurrently(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{
		StreamLogs:    true,
		StreamStats:   true,
		StatsInterval: time.Nanosecond,
	}, func(h *harness) {
		h.dialer.reply = bytes.Repeat([]byte("payload-chunk "), 500)
		h.containers.logData = []byte(strings.Repeat("log line\n", 500))
		h.containers.statsFrames = make([]sandbox.Stats, 100)
		for i := range h.containers.statsFrames {
			h.containers.statsFrames[i] = sandbox.Stats{
				CPUTotal: int64(200 * (i + 1)), PreCPUTotal: int64(100 * (i + 1)),
				Elapsed: time.Second, OnlineCPUs: 4,
			}
		}
		// Widen the window in which a second caller could overlap.
		h.sink.OnPayload = func() { time.Sleep(time.Microsecond) }
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	assert.False(t, h.sink.SawConcurrentUse(),
		"the sink contract promises a single caller; gRPC streams corrupt silently otherwise")
	assert.NotEmpty(t, h.sink.PayloadBytes())
}

// A timeout must surface as an error and destroy the container. The previous
// implementation panicked, recovered, discarded the error, and reported success.
func TestRun_TimeoutFailsAndDestroysTheContainer(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{
		ExecutionTimeout: 150 * time.Millisecond,
		SessionOptions: executor.SessionOptions{
			FirstByteTimeout: time.Hour,
			IdleTimeout:      time.Hour,
		},
	}, func(h *harness) {
		h.dialer.replyDelay = 10 * time.Second
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, execution.ErrExecutionTimeout)

	assert.Equal(t, []container.Outcome{container.OutcomeFailure}, h.containers.Outcomes(),
		"a timed-out container is in an unknown state and must not be reused")
}

func TestRun_ClientCancellationStillReleasesTheContainer(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{
		SessionOptions: executor.SessionOptions{FirstByteTimeout: time.Hour, IdleTimeout: time.Hour},
	}, func(h *harness) {
		h.dialer.replyDelay = 10 * time.Second
	})

	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(100*time.Millisecond, cancel)

	_, err := h.runner.Run(ctx, request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, execution.ErrClientClosed)

	// Cleanup runs on a detached context, so it happens despite the cancellation.
	assert.Equal(t, []container.Outcome{container.OutcomeFailure}, h.containers.Outcomes())
}

// An executor that never closes its connection must not wedge the request.
func TestRun_IdleTimeoutCompletesAHeldOpenResponse(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{
		SessionOptions: executor.SessionOptions{
			FirstByteTimeout: 5 * time.Second,
			IdleTimeout:      150 * time.Millisecond,
		},
	}, func(h *harness) { h.dialer.holdOpen = true })

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)
	assert.Equal(t, "result", string(h.sink.PayloadBytes()))
}

// Logs are a convenience. The previous implementation dereferenced a nil
// reader here and crashed the process from an unrecovered goroutine.
func TestRun_LogFailureIsNotFatal(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{StreamLogs: true}, func(h *harness) {
		h.containers.logsErr = errors.New("container has no log driver")
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)
	assert.Equal(t, "result", string(h.sink.PayloadBytes()))
	assert.Empty(t, h.sink.LogLines())
}

func TestRun_StatsFailureIsNotFatal(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{StreamStats: true}, func(h *harness) {
		h.containers.statsErr = errors.New("stats unavailable")
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)
}

// A followed log stream blocks forever; the runner must still finish and not
// leak the tailing goroutine.
func TestRun_HangingLogStreamDoesNotBlockCompletion(t *testing.T) {
	testutil.AssertNoLeak(t)

	h := newHarness(t, execution.RunnerConfig{StreamLogs: true}, func(h *harness) {
		h.containers.logsHang = true
	})

	done := make(chan error, 1)
	go func() {
		_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
		done <- err
	}()

	select {
	case err := <-done:
		require.NoError(t, err)
	case <-time.After(10 * time.Second):
		t.Fatal("Run did not complete while the log stream was still open")
	}
}

func TestRun_SinkErrorAbortsTheExecution(t *testing.T) {
	testutil.AssertNoLeak(t)

	sinkErr := errors.New("client stream is gone")
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.dialer.reply = bytes.Repeat([]byte("x"), 4*1024*1024)
		h.sink.PayloadErr = sinkErr
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, sinkErr)
	assert.Equal(t, []container.Outcome{container.OutcomeFailure}, h.containers.Outcomes())
}

func TestRun_AcquireFailureSkipsRelease(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.acquireErr = container.ErrAcquireFailed
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, container.ErrAcquireFailed)
	assert.Empty(t, h.containers.Outcomes(), "nothing was acquired, so nothing is released")
}

func TestRun_DialFailureDestroysTheContainer(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.dialer.dialErr = executor.ErrDialTimeout
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrDialTimeout)
	assert.Equal(t, []container.Outcome{container.OutcomeFailure}, h.containers.Outcomes(),
		"a container whose executor never answered is not reusable")
}

func TestRun_RequestSourceErrorFailsTheExecution(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, nil)

	srcErr := errors.New("stream reset")
	_, err := h.runner.Run(context.Background(), request(), &chunkSource{err: srcErr}, h.sink)
	require.Error(t, err)
	assert.ErrorIs(t, err, srcErr)
	assert.Equal(t, []container.Outcome{container.OutcomeFailure}, h.containers.Outcomes())
}

func TestRun_ReusesANamedContainer(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, nil)

	req := request()
	req.ContainerID = "exec_container-warm"

	_, err := h.runner.Run(context.Background(), req, &chunkSource{}, h.sink)
	require.NoError(t, err)

	acquired := h.containers.AcquireRequests()
	require.Len(t, acquired, 1)
	assert.Equal(t, "exec_container-warm", acquired[0].ContainerID)
}

func TestRun_ReportsCPUUtilizationRatherThanMemory(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{
		StreamStats:   true,
		StatsInterval: time.Nanosecond,
	}, func(h *harness) {
		h.containers.statsFrames = []sandbox.Stats{{
			MemoryUsage: 999, MemoryLimit: 4096,
			CPUTotal: 1_400_000_000, PreCPUTotal: 1_000_000_000,
			Elapsed: time.Second, OnlineCPUs: 4,
		}}
	})

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	require.True(t, h.registry.WaitFor(func(calls []fakeregistry.Call) bool {
		for _, c := range calls {
			if c.Method == "ExecutorUtilization" {
				return true
			}
		}
		return false
	}, 2*time.Second))

	for _, c := range h.registry.Calls() {
		if c.Method != "ExecutorUtilization" {
			continue
		}
		// 100ns of container time against 1000ns of host time on 4 CPUs.
		assert.Equal(t, int64(40), c.Utilization.CPUUtil)
		assert.Equal(t, int64(400), c.Utilization.CPUTotal)
		assert.NotEqual(t, int64(999), c.Utilization.CPUUtil, "memory must not be reported as CPU")
		return
	}
	t.Fatal("no utilization report was made")
}

func TestSerialSink_ForwardsEveryMethod(t *testing.T) {
	inner := fakesink.New()
	serial := execution.NewSerialSink(inner)

	require.NoError(t, serial.Provision(execution.ProvisionInfo{ContainerID: "cid"}))
	require.NoError(t, serial.Payload([]byte("data")))
	require.NoError(t, serial.Logs("line\n"))

	assert.Equal(t, []string{"Provision", "Payload", "Logs"}, inner.CallOrder())
	assert.Equal(t, "data", string(inner.PayloadBytes()))
	assert.Equal(t, []string{"line\n"}, inner.LogLines())
}

func TestSerialSink_SerialisesConcurrentCallers(t *testing.T) {
	inner := fakesink.New()
	inner.OnPayload = func() { time.Sleep(time.Millisecond) }
	serial := execution.NewSerialSink(inner)

	var wg sync.WaitGroup
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			assert.NoError(t, serial.Payload([]byte("x")))
		}()
	}
	wg.Wait()

	assert.False(t, inner.SawConcurrentUse())
	assert.Len(t, inner.PayloadBytes(), 20)
}

// A short execution cancels its side pumps before they can open their streams.
// That is routine, so it must not surface as a warning on every fast request.
func TestRun_RoutineSideChannelCancellationIsNotWarned(t *testing.T) {
	var logs bytes.Buffer
	handler := slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug})

	h := newHarness(t, execution.RunnerConfig{StreamLogs: true, StreamStats: true}, func(h *harness) {
		h.containers.logsErr = context.Canceled
		h.containers.statsErr = context.Canceled
	})
	h.runner = execution.NewRunner(h.containers, h.dialer, h.registry,
		execution.RunnerConfig{
			WorkerID: "worker-1", StreamLogs: true, StreamStats: true,
			SessionOptions: executor.SessionOptions{
				FirstByteTimeout: 5 * time.Second,
				IdleTimeout:      200 * time.Millisecond,
			},
		}, slog.New(handler))

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	assert.NotContains(t, logs.String(), "level=WARN",
		"a side channel cancelled by the execution finishing is routine, not a warning")
	assert.Contains(t, logs.String(), "the execution finished first")
}

// A genuine failure to open a side channel is still worth a warning.
func TestRun_RealSideChannelFailureIsWarned(t *testing.T) {
	var logs bytes.Buffer
	handler := slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug})

	h := newHarness(t, execution.RunnerConfig{StreamLogs: true}, func(h *harness) {
		h.containers.logsErr = errors.New("container has no log driver")
	})
	h.runner = execution.NewRunner(h.containers, h.dialer, h.registry,
		execution.RunnerConfig{
			WorkerID: "worker-1", StreamLogs: true,
			SessionOptions: executor.SessionOptions{
				FirstByteTimeout: 5 * time.Second,
				IdleTimeout:      200 * time.Millisecond,
			},
		}, slog.New(handler))

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	assert.Contains(t, logs.String(), "level=WARN")
	assert.Contains(t, logs.String(), "no log driver")
}
