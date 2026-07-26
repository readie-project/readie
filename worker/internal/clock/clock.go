// Package clock abstracts time so retry and sampling loops can be tested
// without real sleeps.
package clock

import (
	"context"
	"sync"
	"time"
)

// Clock provides the subset of the time package the worker depends on.
type Clock interface {
	Now() time.Time
	// Sleep blocks for d or until ctx is done, reporting whether it slept the
	// full duration.
	Sleep(ctx context.Context, d time.Duration) bool
}

// System is a Clock backed by the real time package.
type System struct{}

// NewSystem returns the real clock.
func NewSystem() System { return System{} }

// Now returns the current time.
func (System) Now() time.Time { return time.Now() }

// Sleep blocks for d, returning false if ctx is cancelled first.
func (System) Sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	timer := time.NewTimer(d)
	defer timer.Stop()

	select {
	case <-timer.C:
		return true
	case <-ctx.Done():
		return false
	}
}

var _ Clock = System{}

// Fake is a manually advanced Clock for tests. It is safe for concurrent use.
type Fake struct {
	mu      sync.Mutex
	now     time.Time
	slept   time.Duration
	sleeps  int
	autoAdv bool
}

// NewFake returns a Fake positioned at start. By default Sleep advances the
// clock immediately rather than blocking, so retry loops run at full speed.
func NewFake(start time.Time) *Fake {
	return &Fake{now: start, autoAdv: true}
}

// Now returns the fake's current time.
func (f *Fake) Now() time.Time {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.now
}

// Sleep records the request and, when auto-advance is enabled, moves the clock
// forward without blocking.
func (f *Fake) Sleep(ctx context.Context, d time.Duration) bool {
	if ctx.Err() != nil {
		return false
	}
	f.mu.Lock()
	defer f.mu.Unlock()

	f.sleeps++
	f.slept += d
	if f.autoAdv {
		f.now = f.now.Add(d)
	}
	return true
}

// Advance moves the clock forward.
func (f *Fake) Advance(d time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.now = f.now.Add(d)
}

// SetAutoAdvance controls whether Sleep advances the clock.
func (f *Fake) SetAutoAdvance(enabled bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.autoAdv = enabled
}

// Sleeps reports how many times Sleep was called and the total duration requested.
func (f *Fake) Sleeps() (count int, total time.Duration) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.sleeps, f.slept
}

var _ Clock = (*Fake)(nil)
