package execution

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
)

// FinalizationTimeout is the shutdown drain budget after the RPC server stops.
func (r *Runner) FinalizationTimeout() time.Duration {
	timeout := r.cfg.SessionOptions.TelemetryTimeout
	if timeout <= 0 {
		timeout = executor.TelemetryTimeout
	}
	return r.cfg.ReleaseTimeout + timeout
}

func (r *Runner) waitFinalization(ctx context.Context, id string) error {
	r.finalizeMu.Lock()
	done := r.finalizing[id]
	r.finalizeMu.Unlock()
	if done == nil {
		return nil
	}
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("wait for prior container finalization: %w", ctx.Err())
	}
}

// finalize transfers socket ownership before Run returns. Only the completed
// result path waits for transport completion; errors retain fail-safe cleanup.
func (r *Runner) finalize(ctx context.Context, req Request, handle container.Handle, session *executor.Session,
	outcome container.Outcome, collectTelemetry bool, start, end time.Time, attemptFinished <-chan struct{}, log *slog.Logger) {
	// Drop the request body; retain correlation without live RPC cancellation.
	ctx = context.WithoutCancel(ctx)
	req.InitialPayload = nil
	done := make(chan struct{})
	r.finalizeMu.Lock()
	r.finalizing[handle.ID] = done
	r.finalizeMu.Unlock()
	r.releaseWG.Add(1)
	go func() {
		defer r.releaseWG.Done()
		defer func() {
			r.finalizeMu.Lock()
			if r.finalizing[handle.ID] == done {
				delete(r.finalizing, handle.ID)
			}
			close(done)
			r.finalizeMu.Unlock()
		}()
		defer func() {
			if recover() != nil {
				log.Error("background container finalization panicked")
			}
		}()
		// Even a faulty telemetry implementation must close and release.
		defer r.release(ctx, req, handle, session, outcome, log)
		<-attemptFinished
		if outcome != container.OutcomeSuccess || session == nil {
			return
		}
		if req.ResponseDone != nil {
			<-req.ResponseDone
		}
		if !collectTelemetry {
			return
		}
		select {
		case r.telemetrySlots <- struct{}{}:
			defer func() { <-r.telemetrySlots }()
		default:
			log.Debug("executor telemetry dropped", "reason", "capacity")
			return
		}
		data, err := session.ReadTelemetry(ctx)
		if err != nil {
			log.Debug("executor telemetry dropped", "reason", "read")
			return
		}
		parent := trace.SpanContextFromContext(ctx)
		timings, err := executor.ParseTelemetry(data, start, end, r.cfg.ExecutionTimeout)
		if err != nil {
			log.Debug("executor telemetry dropped", "reason", "invalid")
			return
		}
		r.recordExecutor(parent, req, handle, timings)
	}()
}

func (r *Runner) release(ctx context.Context, req Request, handle container.Handle, session *executor.Session,
	outcome container.Outcome, log *slog.Logger) {
	if session != nil {
		_ = session.Close()
	}
	ctx, cancel := context.WithTimeout(ctx, r.cfg.ReleaseTimeout)
	defer cancel()
	ctx, span := r.trace.Start(ctx, "container release", trace.WithAttributes(
		attribute.String(logging.KeyContainerID, handle.ID), attribute.String("outcome", outcome.String()),
	))
	defer span.End()
	defer func() {
		if recover() != nil {
			span.SetStatus(codes.Error, "container release panicked")
			log.Error("background container release panicked")
		}
	}()
	if err := r.containers.Release(ctx, req.Ref, handle, outcome); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
		log.Error("could not release container", "outcome", outcome.String(), logging.KeyError, err)
	}
}

func (r *Runner) recordExecutor(parent trace.SpanContext, req Request, handle container.Handle, timings executor.Telemetry) {
	ctx := trace.ContextWithSpanContext(context.Background(), parent)
	start := time.Unix(0, timings.StartTimeUnixNS)
	ctx, span := r.trace.Start(ctx, "executor execution", trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithTimestamp(start), trace.WithAttributes(
			attribute.String("readie.telemetry.source", "executor_report"),
			attribute.String(logging.KeyRequestID, req.Ref.RequestID),
			attribute.String(logging.KeySessionID, req.Ref.SessionID),
			attribute.String(logging.KeyWorkerID, r.cfg.WorkerID),
			attribute.String(logging.KeyContainerID, handle.ID),
			attribute.String(logging.KeyCheckpoint, handle.CheckpointID),
			attribute.String("readie.executor.outcome", timings.Outcome),
		))
	defer span.End(trace.WithTimestamp(start.Add(time.Duration(timings.DurationNS))))
	if timings.Outcome != "success" {
		span.SetStatus(codes.Error, timings.Outcome)
	}
	for _, phase := range timings.Phases {
		phaseStart := start.Add(time.Duration(phase.StartOffsetNS))
		_, child := r.trace.Start(ctx, "executor "+phase.Name, trace.WithSpanKind(trace.SpanKindInternal),
			trace.WithTimestamp(phaseStart), trace.WithAttributes(
				attribute.String("readie.telemetry.source", "executor_report"),
				attribute.String("readie.executor.phase", phase.Name),
				attribute.String("readie.executor.outcome", phase.Outcome),
			))
		if phase.Outcome != "success" {
			child.SetStatus(codes.Error, phase.Outcome)
		}
		child.End(trace.WithTimestamp(phaseStart.Add(time.Duration(phase.DurationNS))))
	}
}
