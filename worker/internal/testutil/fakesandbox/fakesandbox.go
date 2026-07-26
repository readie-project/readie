// Package fakesandbox provides an in-memory sandbox.Port for tests.
//
// It tracks state well enough to assert lifecycle transitions (created →
// running → paused → removed), records every CreateSpec so the contract with
// the Python executor can be locked down, and supports per-method error
// injection.
//
// Substituting here rather than at the runsc adapter is what keeps the whole
// integration suite runnable on a development machine: gVisor is Linux-only
// and cannot run under emulation, so no test above this seam may touch a real
// runtime.
package fakesandbox

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/sandbox"
)

// State is a fake sandbox's lifecycle position.
type State struct {
	Spec    sandbox.CreateSpec
	Started bool
	Paused  bool
	Removed bool
	// StartedFrom is the checkpoint the last successful start restored from.
	StartedFrom string
	Memory      int64
}

// CheckpointCall records a snapshot request.
type CheckpointCall struct {
	ID   string
	Spec sandbox.CheckpointSpec
}

// Sandbox is an in-memory sandbox.Port. It is safe for concurrent use.
type Sandbox struct {
	mu          sync.Mutex
	sandboxes   map[string]*State
	order       []string
	creates     []sandbox.CreateSpec
	checkpoints []CheckpointCall
	failures    map[string]error
	closed      bool

	// FailRestore makes any Start carrying a checkpoint id fail, which
	// exercises the manager's downgrade to a cold start.
	FailRestore bool

	// LogData is returned by Logs.
	LogData []byte
	// LogsBlockForever makes Logs return a reader that never yields, standing
	// in for following a live sandbox.
	LogsBlockForever bool

	// StatsFrames are replayed by the stream returned from Stats.
	StatsFrames []sandbox.Stats
	// StatsGap is the delay between replayed frames.
	StatsGap time.Duration
	// StatsHoldOpen keeps the stream open after the last frame instead of
	// reporting io.EOF.
	StatsHoldOpen bool

	// OnCreate runs after a sandbox is recorded, outside the lock. The
	// integration harness uses it to "boot" a fake executor on the sandbox's
	// socket path.
	OnCreate func(id string)
}

var _ sandbox.Port = (*Sandbox)(nil)

// New returns an empty fake.
func New() *Sandbox {
	return &Sandbox{
		sandboxes: make(map[string]*State),
		failures:  make(map[string]error),
	}
}

// FailOn makes the named sandbox.Port method return err. Method names match
// the interface: "Create", "Start", "Stop", "Remove", "Pause", "Unpause",
// "Update", "Checkpoint", "Inspect", "List", "Logs", "Stats", "Probe".
func (d *Sandbox) FailOn(method string, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.failures[method] = err
}

// ClearFailure removes an injected failure.
func (d *Sandbox) ClearFailure(method string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.failures, method)
}

// Seed adds a pre-existing running sandbox, as if left by a previous process.
func (d *Sandbox) Seed(id string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.sandboxes[id] = &State{Started: true, Spec: sandbox.CreateSpec{ID: id}}
	d.order = append(d.order, id)
}

// CreateSpecs returns every spec passed to Create, in order.
func (d *Sandbox) CreateSpecs() []sandbox.CreateSpec {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]sandbox.CreateSpec(nil), d.creates...)
}

// Checkpoints returns every snapshot request, in order.
func (d *Sandbox) Checkpoints() []CheckpointCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]CheckpointCall(nil), d.checkpoints...)
}

// Get returns a copy of a sandbox's state.
func (d *Sandbox) Get(id string) (State, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	s, ok := d.sandboxes[id]
	if !ok {
		return State{}, false
	}
	return *s, true
}

// IDs returns every sandbox ever created, in creation order, including
// removed ones.
func (d *Sandbox) IDs() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.order...)
}

// LiveIDs returns the sandboxes that have not been removed.
func (d *Sandbox) LiveIDs() []string {
	d.mu.Lock()
	defer d.mu.Unlock()

	var live []string
	for _, id := range d.order {
		if s, ok := d.sandboxes[id]; ok && !s.Removed {
			live = append(live, id)
		}
	}
	return live
}

// Closed reports whether Close was called.
func (d *Sandbox) Closed() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.closed
}

// fail returns an injected error for a method, if any. Callers must not hold
// the lock.
func (d *Sandbox) fail(method string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.failures[method]
}

// Create records a sandbox's bundle. Like the real runtime, it starts nothing.
func (d *Sandbox) Create(_ context.Context, spec sandbox.CreateSpec) (string, error) {
	if err := d.fail("Create"); err != nil {
		return "", err
	}

	d.mu.Lock()
	if _, exists := d.sandboxes[spec.ID]; exists {
		d.mu.Unlock()
		return "", sandbox.ErrConflict
	}
	d.creates = append(d.creates, spec)
	d.sandboxes[spec.ID] = &State{Spec: spec, Memory: spec.MemoryBytes}
	d.order = append(d.order, spec.ID)
	onCreate := d.OnCreate
	d.mu.Unlock()

	if onCreate != nil {
		onCreate(spec.ID)
	}
	return spec.ID, nil
}

// Start launches a sandbox, restoring from a checkpoint when asked.
func (d *Sandbox) Start(_ context.Context, id string, spec sandbox.StartSpec) error {
	if err := d.fail("Start"); err != nil {
		return err
	}
	if spec.CheckpointID != "" && d.FailRestore {
		return sandbox.Wrap("restore", id, sandbox.ErrRestoreFailed,
			errors.New("restore is not supported by this runtime"))
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	s.Started = true
	s.Paused = false
	s.StartedFrom = spec.CheckpointID
	return nil
}

// Stop marks a sandbox stopped.
func (d *Sandbox) Stop(_ context.Context, id string, _ time.Duration) error {
	if err := d.fail("Stop"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	s.Started = false
	s.Paused = false
	return nil
}

// Remove deletes a sandbox.
func (d *Sandbox) Remove(_ context.Context, id string, _ bool) error {
	if err := d.fail("Remove"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	s.Removed = true
	s.Started = false
	s.Paused = false
	return nil
}

// Pause suspends a sandbox.
func (d *Sandbox) Pause(_ context.Context, id string) error {
	if err := d.fail("Pause"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	if s.Paused {
		return sandbox.ErrConflict
	}
	s.Paused = true
	return nil
}

// Unpause resumes a sandbox.
func (d *Sandbox) Unpause(_ context.Context, id string) error {
	if err := d.fail("Unpause"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	s.Paused = false
	s.Started = true
	return nil
}

// Update adjusts a sandbox's memory limit.
func (d *Sandbox) Update(_ context.Context, id string, spec sandbox.UpdateSpec) error {
	if err := d.fail("Update"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.ErrNotFound
	}
	s.Memory = spec.MemoryBytes
	return nil
}

// Checkpoint records a snapshot request and writes a placeholder image.
func (d *Sandbox) Checkpoint(_ context.Context, id string, spec sandbox.CheckpointSpec) error {
	if err := d.fail("Checkpoint"); err != nil {
		return err
	}

	d.mu.Lock()
	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		d.mu.Unlock()
		return sandbox.ErrNotFound
	}
	d.checkpoints = append(d.checkpoints, CheckpointCall{ID: id, Spec: spec})
	if !spec.LeaveRunning {
		s.Started = false
	}
	d.mu.Unlock()

	if spec.Dir != "" {
		if err := os.MkdirAll(spec.Dir, 0o755); err != nil {
			return err
		}
		return os.WriteFile(filepath.Join(spec.Dir, "checkpoint.img"), []byte("fake image"), 0o644)
	}
	return nil
}

// Inspect returns a sandbox's state.
func (d *Sandbox) Inspect(_ context.Context, id string) (sandbox.Info, error) {
	if err := d.fail("Inspect"); err != nil {
		return sandbox.Info{}, err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.sandboxes[id]
	if !ok || s.Removed {
		return sandbox.Info{}, sandbox.ErrNotFound
	}
	return sandbox.Info{
		ID:          id,
		PID:         4242,
		MemoryBytes: s.Memory,
		Running:     s.Started,
		Paused:      s.Paused,
	}, nil
}

// List returns live sandboxes whose id starts with idPrefix.
func (d *Sandbox) List(_ context.Context, idPrefix string) ([]sandbox.Summary, error) {
	if err := d.fail("List"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	var summaries []sandbox.Summary
	for _, id := range d.order {
		s := d.sandboxes[id]
		if s.Removed || !strings.HasPrefix(id, idPrefix) {
			continue
		}
		state := "stopped"
		switch {
		case s.Paused:
			state = "paused"
		case s.Started:
			state = "running"
		}
		summaries = append(summaries, sandbox.Summary{ID: id, State: state})
	}
	return summaries, nil
}

// Logs returns the configured log data.
func (d *Sandbox) Logs(ctx context.Context, id string, _ bool) (io.ReadCloser, error) {
	if err := d.fail("Logs"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	_, ok := d.sandboxes[id]
	data := d.LogData
	block := d.LogsBlockForever
	d.mu.Unlock()

	if !ok {
		return nil, sandbox.ErrNotFound
	}
	if block {
		return newBlockingReader(ctx), nil
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

// Stats replays the configured frames.
func (d *Sandbox) Stats(_ context.Context, id string, _ bool) (sandbox.StatsStream, error) {
	if err := d.fail("Stats"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	_, ok := d.sandboxes[id]
	frames := append([]sandbox.Stats(nil), d.StatsFrames...)
	gap, hold := d.StatsGap, d.StatsHoldOpen
	d.mu.Unlock()

	if !ok {
		return nil, sandbox.ErrNotFound
	}
	return &statsStream{frames: frames, gap: gap, hold: hold, closed: make(chan struct{})}, nil
}

// Probe always succeeds unless a failure is injected.
func (d *Sandbox) Probe(context.Context) error { return d.fail("Probe") }

// Close marks the fake closed.
func (d *Sandbox) Close() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.closed = true
	return nil
}

// statsStream replays frames and unblocks Recv on Close.
type statsStream struct {
	frames []sandbox.Stats
	gap    time.Duration
	hold   bool

	mu        sync.Mutex
	i         int
	closeOnce sync.Once
	closed    chan struct{}
}

func (s *statsStream) Recv() (sandbox.Stats, error) {
	s.mu.Lock()
	i, gap := s.i, s.gap
	s.mu.Unlock()

	if gap > 0 && i > 0 {
		timer := time.NewTimer(gap)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-s.closed:
			return sandbox.Stats{}, io.EOF
		}
	}

	select {
	case <-s.closed:
		return sandbox.Stats{}, io.EOF
	default:
	}

	s.mu.Lock()
	if s.i >= len(s.frames) {
		hold := s.hold
		s.mu.Unlock()
		if !hold {
			return sandbox.Stats{}, io.EOF
		}
		// Block until closed, standing in for a live stream with no new data.
		<-s.closed
		return sandbox.Stats{}, io.EOF
	}

	frame := s.frames[s.i]
	s.i++
	s.mu.Unlock()
	return frame, nil
}

func (s *statsStream) Close() error {
	s.closeOnce.Do(func() { close(s.closed) })
	return nil
}

// blockingReader yields nothing until its context ends or it is closed.
type blockingReader struct {
	ctx       context.Context
	done      chan struct{}
	closeOnce sync.Once
}

func newBlockingReader(ctx context.Context) *blockingReader {
	return &blockingReader{ctx: ctx, done: make(chan struct{})}
}

func (b *blockingReader) Read([]byte) (int, error) {
	select {
	case <-b.done:
	case <-b.ctx.Done():
	}
	return 0, io.EOF
}

func (b *blockingReader) Close() error {
	b.closeOnce.Do(func() { close(b.done) })
	return nil
}
