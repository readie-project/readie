package runsc

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"

	"github.com/illinoisdata/readie/worker/internal/sandbox"
)

// event is one `runsc events` document.
//
// gVisor follows runc's shape: a typed envelope whose data carries the usage
// counters. Only the fields the worker uses are declared.
//
// The exact key names are the one part of this adapter that cannot be pinned
// down without a Linux box, which is why parsing lives in a pure function with
// a golden fixture rather than being inlined into the stream.
type event struct {
	Type string    `json:"type"`
	ID   string    `json:"id"`
	Data eventData `json:"data"`
}

type eventData struct {
	Memory eventMemory `json:"memory"`
	CPU    eventCPU    `json:"cpu"`
	Pids   eventPids   `json:"pids"`
}

type eventMemory struct {
	Usage eventMemoryEntry `json:"usage"`
}

type eventMemoryEntry struct {
	Limit uint64 `json:"limit"`
	Usage uint64 `json:"usage"`
	Max   uint64 `json:"max"`
}

type eventCPU struct {
	Usage eventCPUUsage `json:"usage"`
}

type eventCPUUsage struct {
	// Total is cumulative CPU time in nanoseconds.
	Total uint64 `json:"total"`
	// PerCPU lets us report how many CPUs the sandbox sees.
	PerCPU []uint64 `json:"percpu,omitempty"`
	Kernel uint64   `json:"kernel,omitempty"`
	User   uint64   `json:"user,omitempty"`
}

type eventPids struct {
	Current uint64 `json:"current"`
	Limit   uint64 `json:"limit,omitempty"`
}

// parseEvent decodes one events document into a sample.
//
// Delta fields (PreCPUTotal, Elapsed) are the stream's responsibility, since a
// single document carries no history.
func parseEvent(raw []byte, now time.Time) (sandbox.Stats, error) {
	var ev event
	if err := json.Unmarshal(raw, &ev); err != nil {
		return sandbox.Stats{}, fmt.Errorf("parse runsc event: %w", err)
	}

	return sandbox.Stats{
		Read:        now,
		MemoryUsage: int64(ev.Data.Memory.Usage.Usage),
		MemoryLimit: int64(ev.Data.Memory.Usage.Limit),
		CPUTotal:    int64(ev.Data.CPU.Usage.Total),
		OnlineCPUs:  int64(len(ev.Data.CPU.Usage.PerCPU)),
		Pids:        int64(ev.Data.Pids.Current),
	}, nil
}

// pollingStatsStream samples on a ticker by invoking a one-shot command.
//
// gVisor offers a streaming events form, but its flag and output shape are
// unverified on this platform, and one short-lived process per second per
// active execution is affordable. Switching to the streaming form is a
// follow-up once it can be checked against a real runtime.
type pollingStatsStream struct {
	sample   func(context.Context) (sandbox.Stats, error)
	interval time.Duration
	// ctx is held as its done channel rather than as a Context: the stream
	// outlives any single call and has no method to accept one.
	parentDone <-chan struct{}
	sampleCtx  func() (context.Context, context.CancelFunc)

	mu       sync.Mutex
	prevCPU  int64
	prevRead time.Time
	started  bool

	closeOnce sync.Once
	closed    chan struct{}
	once      bool
	done      bool
}

var _ sandbox.StatsStream = (*pollingStatsStream)(nil)

func newPollingStatsStream(
	ctx context.Context,
	sample func(context.Context) (sandbox.Stats, error),
	interval time.Duration,
	once bool,
) *pollingStatsStream {
	return &pollingStatsStream{
		sample:     sample,
		interval:   interval,
		parentDone: ctx.Done(),
		sampleCtx:  func() (context.Context, context.CancelFunc) { return context.WithCancel(ctx) },
		closed:     make(chan struct{}),
		once:       once,
	}
}

func (s *pollingStatsStream) Recv() (sandbox.Stats, error) {
	s.mu.Lock()
	first := !s.started
	finished := s.done
	s.mu.Unlock()

	if finished {
		return sandbox.Stats{}, io.EOF
	}

	if !first {
		timer := time.NewTimer(s.interval)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-s.closed:
			return sandbox.Stats{}, io.EOF
		case <-s.parentDone:
			return sandbox.Stats{}, io.EOF
		}
	}

	sampleCtx, cancel := s.sampleCtx()
	sample, err := s.sample(sampleCtx)
	cancel()
	if err != nil {
		select {
		case <-s.closed:
			return sandbox.Stats{}, io.EOF
		default:
		}
		return sandbox.Stats{}, err
	}

	s.mu.Lock()
	if s.started {
		sample.PreCPUTotal = s.prevCPU
		sample.Elapsed = sample.Read.Sub(s.prevRead)
	}
	s.prevCPU, s.prevRead, s.started = sample.CPUTotal, sample.Read, true
	if s.once {
		s.done = true
	}
	s.mu.Unlock()

	return sample, nil
}

func (s *pollingStatsStream) Close() error {
	s.closeOnce.Do(func() { close(s.closed) })
	return nil
}
