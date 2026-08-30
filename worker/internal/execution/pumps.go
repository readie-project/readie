package execution

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
)

// event is one item of output bound for the Sink.
//
// Payload and log output share a channel so that a single goroutine can own
// the sink; the kind field says which method to call.
type event struct {
	payload []byte
	logs    string
	isLog   bool
}

func (e event) size() int {
	if e.isLog {
		return len(e.logs)
	}
	return len(e.payload)
}

func (e event) deliver(sink Sink) error {
	if e.isLog {
		return sink.Logs(e.logs)
	}
	return sink.Payload(e.payload)
}

// emit publishes an event, abandoning it if the execution is being torn down.
// It reports whether the caller should keep producing.
func emit(ctx context.Context, events chan<- event, ev event) bool {
	select {
	case events <- ev:
		return true
	case <-ctx.Done():
		return false
	}
}

// pumpRequest streams the request body to the executor and terminates it.
//
// io.EOF from the source is the normal end of the caller's body, not an error.
func (r *Runner) pumpRequest(ctx context.Context, req Request, src PayloadSource, session *executor.Session) error {
	if err := session.WriteChunk(req.InitialPayload); err != nil {
		return fmt.Errorf("write initial payload: %w", err)
	}

	for {
		if err := ctx.Err(); err != nil {
			return err
		}

		chunk, err := src.Next(ctx)
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return fmt.Errorf("receive request chunk: %w", err)
		}

		if err := session.WriteChunk(chunk); err != nil {
			return fmt.Errorf("write request chunk: %w", err)
		}
	}

	if err := session.CloseRequest(); err != nil {
		return fmt.Errorf("terminate request: %w", err)
	}
	return nil
}

// pumpResponse relays the executor's reply.
func (r *Runner) pumpResponse(ctx context.Context, session *executor.Session, events chan<- event) error {
	return session.ReadResponse(ctx, func(chunk []byte) error {
		// ReadResponse reuses its buffer between reads, so the chunk must be
		// copied before it travels to another goroutine.
		payload := make([]byte, len(chunk))
		copy(payload, chunk)

		if !emit(ctx, events, event{payload: payload}) {
			return ctx.Err()
		}
		return nil
	})
}

// pumpLogs tails the container's output.
//
// Logs are a convenience, so every failure here is logged and swallowed: the
// previous implementation dereferenced a nil reader when opening logs failed,
// crashing the process from an unrecovered goroutine.
func (r *Runner) pumpLogs(ctx context.Context, containerID string, events chan<- event, log *slog.Logger) {
	logs, err := r.containers.Logs(ctx, containerID)
	if err != nil {
		// A short execution finishes before this stream is even established,
		// which cancels it mid-open. That is the design working, not a problem
		// worth a warning on every fast request.
		logSideChannelFailure(ctx, log, "container logs", err)
		return
	}
	defer func() {
		if err := logs.Close(); err != nil {
			log.Debug("closing container logs", logging.KeyError, err)
		}
	}()

	// Closing the reader is what unblocks a pending Read on cancellation; the
	// deferred Close alone would never run while that Read is in flight.
	stop := context.AfterFunc(ctx, func() { _ = logs.Close() })
	defer stop()

	scanner := bufio.NewScanner(logs)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	for scanner.Scan() {
		line := scanner.Text()
		if !clientVisibleLog(line) {
			continue
		}
		if !emit(ctx, events, event{logs: line + "\n", isLog: true}) {
			return
		}
	}

	if err := scanner.Err(); err != nil && ctx.Err() == nil {
		log.Warn("container log stream ended with an error", logging.KeyError, err)
	}
}

// clientVisibleLog keeps executor lifecycle records in the sandbox log while
// excluding them from the client-facing stream. The runtime exposes one merged
// stdout/stderr file, so this boundary is the only place that can preserve
// diagnostics for operators without presenting startup noise as function output.
// Executor failures deliberately remain visible: they are errors, not lifecycle
// records.
func clientVisibleLog(line string) bool {
	return !strings.HasPrefix(line, "[preimport] ") && !strings.HasPrefix(line, "[checkpoint] ") && !strings.HasPrefix(line, "[executor] ")
}

// pumpStats samples container resource usage, drives proactive memory
// auto-expand, and reports load to the router.
//
// memMax is the request's memory ceiling (0 for "bounded by worker capacity").
// Like logs, this is best-effort and never fails an execution.
func (r *Runner) pumpStats(ctx context.Context, containerID string, memMax int64, log *slog.Logger) {
	stream, err := r.containers.Stats(ctx, containerID)
	if err != nil {
		logSideChannelFailure(ctx, log, "container stats", err)
		return
	}
	defer func() {
		if err := stream.Close(); err != nil {
			log.Debug("closing container stats", logging.KeyError, err)
		}
	}()

	// Close is what interrupts a blocked Recv when the execution ends.
	stop := context.AfterFunc(ctx, func() { _ = stream.Close() })
	defer stop()

	var lastReport time.Time

	for {
		sample, err := stream.Recv()
		if err != nil {
			if !errors.Is(err, io.EOF) && ctx.Err() == nil {
				log.Warn("container stats stream ended with an error", logging.KeyError, err)
			}
			return
		}
		if ctx.Err() != nil {
			return
		}

		// Grow before the container OOMs. The live limit comes off the sample,
		// so this tracks whatever the container is actually running with, and
		// the larger limit the next sample reports is what debounces it.
		r.maybeGrow(ctx, containerID, sample.MemoryUsage, sample.MemoryLimit, memMax, log)

		// The daemon emits a sample per second by default; throttling here
		// keeps a chatty stream from flooding the router.
		now := time.Now()
		if !lastReport.IsZero() && now.Sub(lastReport) < r.cfg.StatsInterval {
			continue
		}
		lastReport = now

		// The router's field is named cpu_util, and CPU utilisation is what
		// now goes into it. The previous implementation put memory usage there.
		util := registry.Utilization{
			CPUUtil:  int64(sample.CPUPercent()),
			CPUTotal: onlineCPUsAsPercent(sample.OnlineCPUs),
		}
		if err := r.reporter.ExecutorUtilization(ctx, containerID, util); err != nil {
			log.Debug("could not report executor utilization", logging.KeyError, err)
		}
	}
}

// maybeGrow raises a container's memory limit when its use approaches the
// limit, up to the ceiling. It is a no-op when no ceiling is known or the
// container is not yet near its limit.
//
// The live cgroup write behind Grow is best-effort: where cgroup delegation is
// unavailable (e.g. Docker Desktop with SANDBOX_IGNORE_CGROUPS), it takes
// effect only on the container's next acquisition, and a hard OOM is caught by
// the reactive retry instead.
func (r *Runner) maybeGrow(ctx context.Context, containerID string, usage, limit, memMax int64, log *slog.Logger) {
	if limit <= 0 || usage < int64(float64(limit)*r.cfg.MemGrowthThreshold) {
		return
	}
	ceiling := r.growthCeiling(memMax)
	if ceiling <= limit {
		return // no room to grow, or no ceiling known
	}
	target := int64(float64(limit) * r.cfg.MemGrowthFactor)
	if target > ceiling {
		target = ceiling
	}
	if target <= limit {
		return
	}
	if err := r.containers.Grow(ctx, containerID, target); err != nil {
		log.Debug("could not grow container memory", "from", limit, "to", target, logging.KeyError, err)
		return
	}
	log.Info("grew container memory", "from", limit, "to", target)
}

// growthCeiling is the largest memory limit auto-expand may reach: the
// request's own max, or the worker's capacity share when the request set none.
// Returns 0 when neither is known, which disables growth.
func (r *Runner) growthCeiling(memMax int64) int64 {
	if memMax > 0 {
		return memMax
	}
	if r.cfg.WorkerMemTotal > 0 {
		return int64(float64(r.cfg.WorkerMemTotal) * r.cfg.MemoryHeadroom)
	}
	return 0
}

// logSideChannelFailure reports a best-effort stream that could not be opened.
//
// Failures caused by the execution already finishing are routine and logged at
// debug; anything else is a genuine warning.
func logSideChannelFailure(ctx context.Context, log *slog.Logger, what string, err error) {
	if ctx.Err() != nil || errors.Is(err, context.Canceled) {
		log.Debug("skipped "+what+"; the execution finished first", logging.KeyError, err)
		return
	}
	log.Warn("could not open "+what+"; continuing without them", logging.KeyError, err)
}

// onlineCPUsAsPercent expresses the container's CPU ceiling on the same scale
// as CPUPercent, where one fully used core is 100.
func onlineCPUsAsPercent(onlineCPUs int64) int64 {
	if onlineCPUs <= 0 {
		return 100
	}
	return onlineCPUs * 100
}
