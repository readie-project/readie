// Package fakesink records execution output and detects concurrent use.
package fakesink

import (
	"bytes"
	"sync"
	"sync/atomic"

	"github.com/illinoisdata/readie/worker/internal/execution"
)

// Sink records everything an execution produces.
//
// Its defining feature is the reentrancy detector: the sink contract promises
// a single caller, and gRPC streams corrupt silently when that is violated, so
// a violation must fail a test rather than manifest as flaky output.
type Sink struct {
	mu         sync.Mutex
	provision  []execution.ProvisionInfo
	payload    bytes.Buffer
	logs       []string
	callOrder  []string
	inCall     atomic.Bool
	reentrance atomic.Bool

	// PayloadErr, when set, is returned from Payload.
	PayloadErr error
	// LogsErr, when set, is returned from Logs.
	LogsErr error
	// ProvisionErr, when set, is returned from Provision.
	ProvisionErr error

	// OnPayload runs inside Payload, while the reentrancy guard is armed.
	OnPayload func()
}

var _ execution.Sink = (*Sink)(nil)

// New returns an empty Sink.
func New() *Sink { return &Sink{} }

// enter arms the reentrancy guard and returns the matching release.
func (s *Sink) enter() func() {
	if !s.inCall.CompareAndSwap(false, true) {
		s.reentrance.Store(true)
	}
	return func() { s.inCall.Store(false) }
}

// Provision records the scheduling decision.
func (s *Sink) Provision(info execution.ProvisionInfo) error {
	defer s.enter()()

	s.mu.Lock()
	s.provision = append(s.provision, info)
	s.callOrder = append(s.callOrder, "Provision")
	s.mu.Unlock()

	return s.ProvisionErr
}

// Payload records a response chunk.
func (s *Sink) Payload(p []byte) error {
	defer s.enter()()

	if s.OnPayload != nil {
		s.OnPayload()
	}

	s.mu.Lock()
	s.payload.Write(p)
	s.callOrder = append(s.callOrder, "Payload")
	s.mu.Unlock()

	return s.PayloadErr
}

// Logs records a log line.
func (s *Sink) Logs(msg string) error {
	defer s.enter()()

	s.mu.Lock()
	s.logs = append(s.logs, msg)
	s.callOrder = append(s.callOrder, "Logs")
	s.mu.Unlock()

	return s.LogsErr
}

// SawConcurrentUse reports whether two calls ever overlapped.
func (s *Sink) SawConcurrentUse() bool { return s.reentrance.Load() }

// PayloadBytes returns the concatenated response body.
func (s *Sink) PayloadBytes() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]byte(nil), s.payload.Bytes()...)
}

// LogLines returns the log lines received.
func (s *Sink) LogLines() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.logs...)
}

// Provisions returns the provisioning calls received.
func (s *Sink) Provisions() []execution.ProvisionInfo {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]execution.ProvisionInfo(nil), s.provision...)
}

// CallOrder returns the method names in the order they were called.
func (s *Sink) CallOrder() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.callOrder...)
}
