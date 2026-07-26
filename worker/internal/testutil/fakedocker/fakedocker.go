// Package fakedocker provides an in-memory docker.Port for tests.
//
// It tracks container state well enough to assert lifecycle transitions
// (created → running → paused → removed), records every CreateSpec so the
// contract with the Python executor can be locked down, and supports per-method
// error injection.
package fakedocker

import (
	"bytes"
	"errors"
	"io"
	"sync"
	"time"

	"context"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/docker"
)

// State is a fake container's lifecycle position.
type State struct {
	Spec    docker.CreateSpec
	Started bool
	Paused  bool
	Removed bool
	// StartedFrom is the checkpoint the last successful start used.
	StartedFrom string
	Memory      int64
}

// Docker is an in-memory docker.Port. It is safe for concurrent use.
type Docker struct {
	mu         sync.Mutex
	containers map[string]*State
	order      []string
	creates    []docker.CreateSpec
	failures   map[string]error
	closed     bool

	// FailCheckpointStart makes any Start carrying a checkpoint id fail, which
	// exercises the manager's downgrade to a cold start.
	FailCheckpointStart bool

	// LogData is returned by Logs.
	LogData []byte
	// LogsBlockForever makes Logs return a reader that never yields, standing
	// in for a followed stream.
	LogsBlockForever bool

	// StatsFrames are replayed by the stream returned from Stats.
	StatsFrames []docker.Stats
	// StatsGap is the delay between replayed frames.
	StatsGap time.Duration
	// StatsHoldOpen keeps the stream open after the last frame instead of
	// reporting io.EOF.
	StatsHoldOpen bool

	// OnCreate runs after a container is recorded, outside the lock. The
	// integration harness uses it to "boot" a fake executor on the container's
	// socket path.
	OnCreate func(id string)
}

var _ docker.Port = (*Docker)(nil)

// New returns an empty fake.
func New() *Docker {
	return &Docker{
		containers: make(map[string]*State),
		failures:   make(map[string]error),
	}
}

// FailOn makes the named docker.Port method return err. Method names match the
// interface: "Create", "Start", "Stop", "Remove", "Pause", "Unpause", "Update",
// "Inspect", "List", "Logs", "Stats", "Ping".
func (d *Docker) FailOn(method string, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.failures[method] = err
}

// ClearFailure removes an injected failure.
func (d *Docker) ClearFailure(method string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.failures, method)
}

// Seed adds a pre-existing container, as if left behind by a previous process.
func (d *Docker) Seed(id string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.containers[id] = &State{Started: true, Spec: docker.CreateSpec{Name: id}}
	d.order = append(d.order, id)
}

// CreateSpecs returns every spec passed to Create, in order.
func (d *Docker) CreateSpecs() []docker.CreateSpec {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]docker.CreateSpec(nil), d.creates...)
}

// Get returns a copy of a container's state.
func (d *Docker) Get(id string) (State, bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	s, ok := d.containers[id]
	if !ok {
		return State{}, false
	}
	return *s, true
}

// IDs returns every container ever created, in creation order, including
// removed ones.
func (d *Docker) IDs() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]string(nil), d.order...)
}

// LiveIDs returns the containers that have not been removed.
func (d *Docker) LiveIDs() []string {
	d.mu.Lock()
	defer d.mu.Unlock()

	var live []string
	for _, id := range d.order {
		if s, ok := d.containers[id]; ok && !s.Removed {
			live = append(live, id)
		}
	}
	return live
}

// Closed reports whether Close was called.
func (d *Docker) Closed() bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.closed
}

// fail returns an injected error for a method, if any. Callers must not hold
// the lock.
func (d *Docker) fail(method string) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.failures[method]
}

// Create records a container.
func (d *Docker) Create(_ context.Context, spec docker.CreateSpec) (string, error) {
	if err := d.fail("Create"); err != nil {
		return "", err
	}

	d.mu.Lock()
	if _, exists := d.containers[spec.Name]; exists {
		d.mu.Unlock()
		return "", docker.ErrConflict
	}
	d.creates = append(d.creates, spec)
	d.containers[spec.Name] = &State{Spec: spec, Memory: spec.MemoryBytes}
	d.order = append(d.order, spec.Name)
	onCreate := d.OnCreate
	d.mu.Unlock()

	if onCreate != nil {
		onCreate(spec.Name)
	}
	return spec.Name, nil
}

// Start marks a container running.
func (d *Docker) Start(_ context.Context, id string, spec docker.StartSpec) error {
	if err := d.fail("Start"); err != nil {
		return err
	}
	if spec.CheckpointID != "" && d.FailCheckpointStart {
		return errors.New("checkpoint restore is not supported by this daemon")
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	s.Started = true
	s.Paused = false
	s.StartedFrom = spec.CheckpointID
	return nil
}

// Stop marks a container stopped.
func (d *Docker) Stop(_ context.Context, id string, _ time.Duration) error {
	if err := d.fail("Stop"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	s.Started = false
	s.Paused = false
	return nil
}

// Remove deletes a container.
func (d *Docker) Remove(_ context.Context, id string, _ bool) error {
	if err := d.fail("Remove"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	s.Removed = true
	s.Started = false
	s.Paused = false
	return nil
}

// Pause suspends a container.
func (d *Docker) Pause(_ context.Context, id string) error {
	if err := d.fail("Pause"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	if s.Paused {
		return docker.ErrConflict
	}
	s.Paused = true
	return nil
}

// Unpause resumes a container.
func (d *Docker) Unpause(_ context.Context, id string) error {
	if err := d.fail("Unpause"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	s.Paused = false
	s.Started = true
	return nil
}

// Update adjusts a container's memory limit.
func (d *Docker) Update(_ context.Context, id string, spec docker.UpdateSpec) error {
	if err := d.fail("Update"); err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.ErrNotFound
	}
	s.Memory = spec.MemoryBytes
	return nil
}

// Inspect returns a container's state.
func (d *Docker) Inspect(_ context.Context, id string) (docker.Info, error) {
	if err := d.fail("Inspect"); err != nil {
		return docker.Info{}, err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	s, ok := d.containers[id]
	if !ok || s.Removed {
		return docker.Info{}, docker.ErrNotFound
	}
	return docker.Info{
		ID:          id,
		Name:        id,
		Image:       s.Spec.Image,
		MemoryBytes: s.Memory,
		Running:     s.Started,
		Paused:      s.Paused,
	}, nil
}

// List returns live containers whose name starts with prefix.
func (d *Docker) List(_ context.Context, namePrefix string) ([]docker.Summary, error) {
	if err := d.fail("List"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	var summaries []docker.Summary
	for _, id := range d.order {
		s := d.containers[id]
		if s.Removed || len(id) < len(namePrefix) || id[:len(namePrefix)] != namePrefix {
			continue
		}
		state := "exited"
		switch {
		case s.Paused:
			state = "paused"
		case s.Started:
			state = "running"
		}
		summaries = append(summaries, docker.Summary{ID: "docker-" + id, Names: []string{id}, State: state})
	}
	return summaries, nil
}

// Logs returns the configured log data.
func (d *Docker) Logs(ctx context.Context, id string, _ bool) (io.ReadCloser, error) {
	if err := d.fail("Logs"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	_, ok := d.containers[id]
	data := d.LogData
	block := d.LogsBlockForever
	d.mu.Unlock()

	if !ok {
		return nil, docker.ErrNotFound
	}
	if block {
		return newBlockingReader(ctx), nil
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

// Stats replays the configured frames.
func (d *Docker) Stats(_ context.Context, id string, _ bool) (docker.StatsStream, error) {
	if err := d.fail("Stats"); err != nil {
		return nil, err
	}

	d.mu.Lock()
	_, ok := d.containers[id]
	frames := append([]docker.Stats(nil), d.StatsFrames...)
	gap, hold := d.StatsGap, d.StatsHoldOpen
	d.mu.Unlock()

	if !ok {
		return nil, docker.ErrNotFound
	}
	return &statsStream{frames: frames, gap: gap, hold: hold, closed: make(chan struct{})}, nil
}

// Ping always succeeds unless a failure is injected.
func (d *Docker) Ping(context.Context) error { return d.fail("Ping") }

// Close marks the fake closed.
func (d *Docker) Close() error {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.closed = true
	return nil
}

// statsStream replays frames and unblocks Recv on Close.
type statsStream struct {
	frames []docker.Stats
	gap    time.Duration
	hold   bool

	mu        sync.Mutex
	i         int
	closeOnce sync.Once
	closed    chan struct{}
}

func (s *statsStream) Recv() (docker.Stats, error) {
	s.mu.Lock()
	i, gap := s.i, s.gap
	s.mu.Unlock()

	if gap > 0 && i > 0 {
		timer := time.NewTimer(gap)
		defer timer.Stop()
		select {
		case <-timer.C:
		case <-s.closed:
			return docker.Stats{}, io.EOF
		}
	}

	select {
	case <-s.closed:
		return docker.Stats{}, io.EOF
	default:
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	if s.i >= len(s.frames) {
		if !s.hold {
			return docker.Stats{}, io.EOF
		}
		// Block until closed, standing in for a live stream with no new data.
		s.mu.Unlock()
		<-s.closed
		s.mu.Lock()
		return docker.Stats{}, io.EOF
	}

	frame := s.frames[s.i]
	s.i++
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
