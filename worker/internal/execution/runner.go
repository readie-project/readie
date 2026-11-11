package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"golang.org/x/sync/errgroup"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
	"github.com/illinoisdata/readie/worker/internal/registry"
)

// RunnerConfig tunes execution behaviour.
type RunnerConfig struct {
	WorkerID string

	// ExecutionTimeout bounds one execution end to end.
	ExecutionTimeout time.Duration
	// ReleaseTimeout bounds container cleanup, which runs in the background on
	// a context detached from the execution's own and from the RPC that
	// requested it: the response no longer waits on cleanup finishing.
	ReleaseTimeout time.Duration
	// StatusTimeout bounds a router status report.
	StatusTimeout time.Duration

	SessionOptions executor.SessionOptions
	// TelemetryCollectors bounds concurrent post-response trailer work.
	TelemetryCollectors int

	// StreamLogs and StreamStats enable the best-effort side channels.
	StreamLogs  bool
	StreamStats bool
	// StatsInterval throttles utilization reports to the router.
	StatsInterval time.Duration

	// Memory auto-expand. When a container's memory use crosses
	// MemGrowthThreshold of its limit, the stats watcher raises the limit by
	// MemGrowthFactor, up to the request's max budget or, when it sets none,
	// WorkerMemTotal * MemoryHeadroom. Zero WorkerMemTotal with no per-request
	// max means no ceiling is known, so growth is skipped.
	MemGrowthThreshold float64
	MemGrowthFactor    float64
	WorkerMemTotal     int64
	MemoryHeadroom     float64

	// EventBuffer sizes the channel between the producers and the sink. A
	// small buffer smooths bursts without letting a slow caller accumulate
	// unbounded output in memory.
	EventBuffer int
}

func (c RunnerConfig) withDefaults() RunnerConfig {
	if c.ExecutionTimeout <= 0 {
		c.ExecutionTimeout = time.Hour
	}
	if c.ReleaseTimeout <= 0 {
		c.ReleaseTimeout = 30 * time.Second
	}
	if c.StatusTimeout <= 0 {
		c.StatusTimeout = 5 * time.Second
	}
	if c.StatsInterval <= 0 {
		c.StatsInterval = time.Second
	}
	if c.EventBuffer <= 0 {
		c.EventBuffer = 16
	}
	if c.MemGrowthThreshold <= 0 || c.MemGrowthThreshold > 1 {
		c.MemGrowthThreshold = 0.9
	}
	if c.MemGrowthFactor <= 1 {
		c.MemGrowthFactor = 2.0
	}
	if c.MemoryHeadroom <= 0 || c.MemoryHeadroom > 1 {
		c.MemoryHeadroom = 0.9
	}
	if c.TelemetryCollectors <= 0 {
		c.TelemetryCollectors = 64
	}
	return c
}

// Runner executes one request against one container.
type Runner struct {
	containers ContainerService
	dialer     executor.Dialer
	reporter   registry.Reporter
	cfg        RunnerConfig
	log        *slog.Logger
	trace      trace.Tracer

	// releaseWG tracks telemetry collection and container releases in the background
	// after their owning RPC has already returned. Shutdown waits on it
	// (bounded) before sweeping orphans, so a straggling Destroy isn't raced
	// by that sweep as a matter of course.
	releaseWG      sync.WaitGroup
	finalizeMu     sync.Mutex
	finalizing     map[string]chan struct{}
	telemetrySlots chan struct{}
}

// NewRunner builds a Runner.
func NewRunner(
	containers ContainerService,
	dialer executor.Dialer,
	reporter registry.Reporter,
	cfg RunnerConfig,
	log *slog.Logger,
	tracer trace.Tracer,
) *Runner {
	if log == nil {
		log = slog.Default()
	}
	if tracer == nil {
		tracer = noop.NewTracerProvider().Tracer("worker")
	}
	cfg = cfg.withDefaults()
	return &Runner{
		finalizing:     make(map[string]chan struct{}),
		telemetrySlots: make(chan struct{}, cfg.TelemetryCollectors),
		containers:     containers,
		dialer:         dialer,
		reporter:       reporter,
		cfg:            cfg,
		log:            log,
		trace:          tracer,
	}
}

// Run executes a request, retrying once with a larger memory limit if the
// first attempt dies in a way consistent with an out-of-memory kill.
//
// The retry is the reactive half of auto-expand: where a live cgroup grow
// cannot take effect the container is killed before the watcher can react, and
// a fresh container created with a bigger limit is what saves the request. It
// is strictly bounded - one retry, only on an OOM-shaped failure, only when
// there is room to grow - because re-running is unsafe for a function with side
// effects. The request body is buffered so it can be replayed on the retry.
//
// Never retried: a request naming an existing ContainerID (a session resume).
// A failed attempt's container is always released as OutcomeFailure, which
// destroys it in the background - so retrying against the same ID races that
// teardown, and even a retry that wins the race cannot recover a session's
// accumulated in-container state anyway. Growing memory only ever helps a
// fresh container, which this always has room to create.
func (r *Runner) Run(ctx context.Context, req Request, src PayloadSource, sink Sink) (Result, error) {
	if err := r.waitFinalization(ctx, req.ContainerID); err != nil {
		return Result{}, err
	}
	rec := &recordingSource{inner: src}

	result, err := r.runOnce(ctx, req, rec, sink)
	if err == nil || ctx.Err() != nil || !looksLikeOOM(err) || req.ContainerID != "" {
		return result, err
	}

	grown, ok := r.grownRequest(req)
	if !ok {
		return result, err // no room to grow; report the original failure
	}
	r.log.Warn("execution failed as if out of memory; retrying with a larger limit",
		logging.KeyRequestID, req.Ref.RequestID,
		"from", req.Alloc.Memory().Alloc, "to", grown.Alloc.Memory().Alloc,
		logging.KeyError, err)

	trace.SpanFromContext(ctx).AddEvent("execution retrying after possible OOM", trace.WithAttributes(
		attribute.Int64("from", req.Alloc.Memory().Alloc),
		attribute.Int64("to", grown.Alloc.Memory().Alloc),
	))
	rec.rewind()
	return r.runOnce(ctx, grown, rec, sink)
}

// WaitPendingReleases blocks until every container release started by a past
// runOnce, including its telemetry collection, has finished, or ctx is done.
//
// It exists for shutdown: the RPC response no longer waits on teardown, so
// the gRPC server draining is no longer proof that a request's container is
// gone. This gives the caller a bounded window to let straggling releases
// land before CleanupOrphans double-checks the runtime. Each release
// has its own ReleaseTimeout; callers should allow FinalizationTimeout to
// include the trailer budget as well.
func (r *Runner) WaitPendingReleases(ctx context.Context) error {
	done := make(chan struct{})
	go func() {
		r.releaseWG.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// looksLikeOOM reports whether a failure is consistent with the executor being
// OOM-killed: it died without a complete response. This is a heuristic - the
// runtime exposes no OOM signal today - so it only ever triggers a single,
// bounded retry.
func looksLikeOOM(err error) bool {
	return errors.Is(err, executor.ErrNoResponse) || errors.Is(err, executor.ErrTruncatedResponse)
}

// grownRequest returns req with its memory budget expanded toward its ceiling,
// and whether there was any room to grow.
func (r *Runner) grownRequest(req Request) (Request, bool) {
	mem := req.Alloc.Memory()
	if mem.Alloc <= 0 {
		return req, false // no known size to grow from
	}
	ceiling := r.growthCeiling(mem.Max)
	if ceiling <= mem.Alloc {
		return req, false
	}
	target := int64(float64(mem.Alloc) * r.cfg.MemGrowthFactor)
	if target > ceiling {
		target = ceiling
	}
	if target <= mem.Alloc {
		return req, false
	}

	budgets := make([]container.Budget, 0, len(req.Alloc.Budgets)+1)
	replaced := false
	for _, b := range req.Alloc.Budgets {
		if b.Kind == container.KindMemory {
			b.Alloc = target
			replaced = true
		}
		budgets = append(budgets, b)
	}
	if !replaced {
		budgets = append(budgets, container.Budget{Kind: container.KindMemory, Alloc: target})
	}
	req.Alloc = container.Allocation{Budgets: budgets}
	return req, true
}

// runOnce executes a request and relays its output to sink.
//
// Concurrency shape: the response reader feeds a single events channel, and
// only this goroutine
// drains it into sink. That single-owner rule is what makes the sink safe
// without locking; the previous implementation sent to the gRPC stream from
// two goroutines at once, which is a data race by construction.
//
// Cleanup: the container is released through a deferred call reading an
// outcome variable whose zero value destroys it. Success has to be recorded
// explicitly on the final line, so every early return - including a timeout -
// fails safe.
func (r *Runner) runOnce(ctx context.Context, req Request, src PayloadSource, sink Sink) (result Result, runErr error) {
	attemptStart := time.Now()
	attemptFinished := make(chan struct{})
	ctx, span := r.trace.Start(ctx, "execution attempt", trace.WithAttributes(
		attribute.String(logging.KeyWorkerID, r.cfg.WorkerID),
		attribute.String(logging.KeyRequestID, req.Ref.RequestID),
		attribute.String(logging.KeySessionID, req.Ref.SessionID),
		attribute.Int64("mem_alloc", req.Alloc.Memory().Alloc),
	))
	// Finalization runs after this span ends, when IsRecording is always false.
	collectTelemetry := span.IsRecording() && span.SpanContext().IsSampled()
	defer func() {
		span.SetAttributes(attribute.Int("bytes_sent", result.BytesSent))
		endSpan(span, runErr)
		close(attemptFinished)
	}()

	log := r.log.With(
		logging.KeyRequestID, req.Ref.RequestID,
		logging.KeySessionID, req.Ref.SessionID,
	)

	execCtx, cancel := context.WithTimeout(ctx, r.cfg.ExecutionTimeout)
	defer cancel()

	acquireCtx, acquireSpan := r.trace.Start(execCtx, "container acquire", trace.WithAttributes(
		attribute.String("requested_container_id", req.ContainerID),
		attribute.String("requested_checkpoint_id", req.CheckpointID),
	))
	handle, err := r.containers.Acquire(acquireCtx, req.toAcquire())
	endSpan(acquireSpan, err)
	if err != nil {
		return Result{}, fmt.Errorf("acquire container: %w", err)
	}
	span.SetAttributes(
		attribute.String(logging.KeyContainerID, handle.ID),
		attribute.String(logging.KeyCheckpoint, handle.CheckpointID),
	)
	span.AddEvent("container acquired", trace.WithAttributes(
		attribute.String(logging.KeyContainerID, handle.ID),
		attribute.String(logging.KeyCheckpoint, handle.CheckpointID),
		attribute.Bool("warm", req.ContainerID != ""),
		attribute.Int64("mem_alloc", handle.Alloc.Memory().Alloc),
	))
	log = log.With(logging.KeyContainerID, handle.ID)

	outcome := container.OutcomeFailure
	var session *executor.Session
	defer func() {
		r.finalize(ctx, req, handle, session, outcome, collectTelemetry, attemptStart, time.Now(), attemptFinished, log)
	}()

	// The router reads the scheduling decision off the first response, so the
	// sink learns it before any output is produced.
	if provisionErr := sink.Provision(r.provisionInfo(req, handle)); provisionErr != nil {
		return Result{}, fmt.Errorf("provision sink: %w", provisionErr)
	}

	dialCtx, dialSpan := r.trace.Start(execCtx, "executor dial")
	conn, err := r.dialer.Dial(dialCtx, handle.ID)
	endSpan(dialSpan, err)
	if err != nil {
		return Result{}, fmt.Errorf("dial executor: %w", err)
	}
	span.AddEvent("executor connected")
	session = executor.NewSession(conn, r.cfg.SessionOptions, log)

	events := make(chan event, r.cfg.EventBuffer)

	// The primary group ends at the result terminator. The stats side group
	// follows an open-ended stream, so cancel it rather than waiting for it
	// to end naturally. Telemetry belongs to the post-RPC finalizer.
	primary, primaryCtx := errgroup.WithContext(execCtx)
	primary.Go(func() error { return r.pumpRequest(primaryCtx, req, src, session) })
	primary.Go(func() error { return r.pumpResponse(primaryCtx, session, events) })

	sideCtx, cancelSide := context.WithCancel(execCtx)
	defer cancelSide()
	var side errgroup.Group

	if r.cfg.StreamStats {
		memMax := handle.Alloc.Memory().Max
		side.Go(func() error { r.pumpStats(sideCtx, handle.ID, memMax, log); return nil })
	}

	// Closing events is what ends the drain loop below, so it must happen only
	// once every possible sender has returned.
	var waitErr error
	waited := make(chan struct{})
	go func() {
		defer close(waited)
		waitErr = primary.Wait()
		cancelSide()
		_ = side.Wait()
		close(events)
	}()

	// Sole owner of sink.
	var (
		sendErr   error
		bytesSent int
		responded bool
	)
	for ev := range events {
		if sendErr != nil {
			// Keep draining: a producer blocked on a full channel would never
			// observe cancellation and the group would never finish.
			continue
		}
		bytesSent += ev.size()
		if sendErr = ev.deliver(sink); sendErr != nil {
			// Tear the producers down rather than continuing to pull output
			// nobody can receive.
			cancel()
		} else if !responded {
			span.AddEvent("response started")
			responded = true
		}
	}
	<-waited // establishes the happens-before edge for reading waitErr

	if err := r.classify(ctx, execCtx, sendErr, waitErr); err != nil {
		return Result{}, err
	}

	outcome = container.OutcomeSuccess
	return Result{Handle: handle, BytesSent: bytesSent}, nil
}

// endSpan records the operation's own error before ending its span.
func endSpan(span trace.Span, err error) {
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
}

// classify turns the various failure signals into one error, preferring the
// most specific explanation available.
//
// ctx/execCtx are consulted only to explain an actual failure signal
// (sendErr or waitErr), never as a failure signal by themselves: a caller
// commonly ends its own context the moment it has fully received a response
// (closing the stream right after its last message, or simply because
// execCtx's deadline lapses a beat later), and by then the response has
// already gone out. Treating that as a failure destroyed a container a
// session meant to keep resuming - readable in the router as a fine
// checkpoint restore immediately followed, seconds later while the container
// only sat idle, by a request for a container that no longer existed.
func (r *Runner) classify(ctx, execCtx context.Context, sendErr, waitErr error) error {
	if sendErr == nil && waitErr == nil {
		return nil
	}
	// A cancelled caller is the reason behind most downstream errors, so it is
	// checked before them.
	if ctx.Err() != nil {
		return fmt.Errorf("%w: %w", ErrClientClosed, ctx.Err())
	}
	if errors.Is(execCtx.Err(), context.DeadlineExceeded) {
		return ErrExecutionTimeout
	}
	if sendErr != nil {
		return fmt.Errorf("relay response: %w", sendErr)
	}
	return waitErr
}

// recordingSource buffers the request body so it can be replayed on a retry.
//
// Next records each chunk it reads from inner; rewind replays the recorded
// chunks before reading further ones, so a second attempt sees the same body
// followed by whatever had not yet arrived when the first attempt failed.
type recordingSource struct {
	inner PayloadSource
	buf   [][]byte
	pos   int
}

func (rs *recordingSource) Next(ctx context.Context) ([]byte, error) {
	if rs.pos < len(rs.buf) {
		chunk := rs.buf[rs.pos]
		rs.pos++
		return chunk, nil
	}
	chunk, err := rs.inner.Next(ctx)
	if err != nil {
		return nil, err //nolint:wrapcheck // preserve io.EOF and the inner error
	}
	saved := make([]byte, len(chunk))
	copy(saved, chunk)
	rs.buf = append(rs.buf, saved)
	rs.pos = len(rs.buf)
	return saved, nil
}

func (rs *recordingSource) rewind() { rs.pos = 0 }

func (r *Runner) provisionInfo(req Request, h container.Handle) ProvisionInfo {
	return ProvisionInfo{
		Ref:          req.Ref,
		WorkerID:     r.cfg.WorkerID,
		ContainerID:  h.ID,
		CheckpointID: h.CheckpointID,
		Budgets:      h.Alloc.Budgets,
	}
}
