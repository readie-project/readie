package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/container"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
)

// RunnerConfig tunes execution behaviour.
type RunnerConfig struct {
	WorkerID string

	// ExecutionTimeout bounds one execution end to end.
	ExecutionTimeout time.Duration
	// ReleaseTimeout bounds container cleanup, which runs on a context
	// detached from the execution's own.
	ReleaseTimeout time.Duration
	// StatusTimeout bounds a router status report.
	StatusTimeout time.Duration

	SessionOptions executor.SessionOptions

	// StreamLogs and StreamStats enable the best-effort side channels.
	StreamLogs  bool
	StreamStats bool
	// StatsInterval throttles utilization reports to the router.
	StatsInterval time.Duration

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
	return c
}

// Runner executes one request against one container.
type Runner struct {
	containers ContainerService
	dialer     executor.Dialer
	reporter   registry.Reporter
	cfg        RunnerConfig
	log        *slog.Logger
}

// NewRunner builds a Runner.
func NewRunner(
	containers ContainerService,
	dialer executor.Dialer,
	reporter registry.Reporter,
	cfg RunnerConfig,
	log *slog.Logger,
) *Runner {
	if log == nil {
		log = slog.Default()
	}
	return &Runner{
		containers: containers,
		dialer:     dialer,
		reporter:   reporter,
		cfg:        cfg.withDefaults(),
		log:        log,
	}
}

// Run executes a request and relays its output to sink.
//
// Concurrency shape: several producers (the response reader, the log tailer,
// the stats sampler) feed a single events channel, and only this goroutine
// drains it into sink. That single-owner rule is what makes the sink safe
// without locking; the previous implementation sent to the gRPC stream from
// two goroutines at once, which is a data race by construction.
//
// Cleanup: the container is released through a deferred call reading an
// outcome variable whose zero value destroys it. Success has to be recorded
// explicitly on the final line, so every early return — including a timeout —
// fails safe.
func (r *Runner) Run(ctx context.Context, req Request, src PayloadSource, sink Sink) (Result, error) {
	log := r.log.With(
		logging.KeyRequestID, req.Ref.RequestID,
		logging.KeySessionID, req.Ref.SessionID,
	)

	execCtx, cancel := context.WithTimeout(ctx, r.cfg.ExecutionTimeout)
	defer cancel()

	handle, err := r.containers.Acquire(execCtx, req.toAcquire())
	if err != nil {
		return Result{}, fmt.Errorf("acquire container: %w", err)
	}
	log = log.With(logging.KeyContainerID, handle.ID)

	outcome := container.OutcomeFailure // zero value, restated for emphasis
	defer func() {
		// Cleanup must survive the very deadline that caused it, so it runs on
		// a context detached from execCtx.
		releaseCtx, releaseCancel := context.WithTimeout(
			context.WithoutCancel(ctx), r.cfg.ReleaseTimeout)
		defer releaseCancel()

		if releaseErr := r.containers.Release(releaseCtx, req.Ref, handle, outcome); releaseErr != nil {
			log.Error("could not release container", "outcome", outcome.String(), logging.KeyError, releaseErr)
		}
	}()

	// The router reads the scheduling decision off the first response, so the
	// sink learns it before any output is produced.
	if provisionErr := sink.Provision(r.provisionInfo(req, handle)); provisionErr != nil {
		return Result{}, fmt.Errorf("provision sink: %w", provisionErr)
	}

	conn, err := r.dialer.Dial(execCtx, handle.ID)
	if err != nil {
		return Result{}, fmt.Errorf("dial executor: %w", err)
	}
	session := executor.NewSession(conn, r.cfg.SessionOptions, log)
	defer func() {
		if err := session.Close(); err != nil {
			log.Debug("closing executor session", logging.KeyError, err)
		}
	}()

	events := make(chan event, r.cfg.EventBuffer)

	// The pumps are split into two groups by lifetime. The primary group does
	// the work the execution is for; the side group carries logs and stats,
	// which follow open-ended streams that only end when the container does.
	// Waiting on both together would deadlock: a log tail blocks until the
	// execution is cancelled, and the execution is not cancelled until it has
	// finished waiting.
	primary, primaryCtx := errgroup.WithContext(execCtx)
	primary.Go(func() error { return r.pumpRequest(primaryCtx, req, src, session) })
	primary.Go(func() error { return r.pumpResponse(primaryCtx, session, events) })

	sideCtx, cancelSide := context.WithCancel(execCtx)
	defer cancelSide()
	var side errgroup.Group

	if r.cfg.StreamLogs {
		side.Go(func() error { r.pumpLogs(sideCtx, handle.ID, events, log); return nil })
	}
	if r.cfg.StreamStats {
		side.Go(func() error { r.pumpStats(sideCtx, handle.ID, log); return nil })
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
		}
	}
	<-waited // establishes the happens-before edge for reading waitErr

	if err := r.classify(ctx, execCtx, sendErr, waitErr); err != nil {
		return Result{}, err
	}

	outcome = container.OutcomeSuccess
	return Result{Handle: handle, BytesSent: bytesSent}, nil
}

// classify turns the various failure signals into one error, preferring the
// most specific explanation available.
func (r *Runner) classify(ctx, execCtx context.Context, sendErr, waitErr error) error {
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
	if waitErr != nil {
		return waitErr
	}
	return nil
}

func (r *Runner) provisionInfo(req Request, h container.Handle) ProvisionInfo {
	return ProvisionInfo{
		Ref:          req.Ref,
		WorkerID:     r.cfg.WorkerID,
		ContainerID:  h.ID,
		CheckpointID: h.CheckpointID,
		CPUAlloc:     h.Alloc.CPUAlloc,
		GPUAlloc:     h.Alloc.GPUAlloc,
	}
}
