package execution

import "sync"

// SerialSink serialises access to a Sink.
//
// The Runner already guarantees a single caller, so this is defence in depth
// rather than a load-bearing lock: it means a future change that introduces a
// second producer degrades into contention instead of a data race on a gRPC
// stream, which manifests as corrupted frames rather than a clean failure.
type SerialSink struct {
	mu    sync.Mutex
	inner Sink
}

var _ Sink = (*SerialSink)(nil)

// NewSerialSink wraps a Sink in a mutex.
func NewSerialSink(inner Sink) *SerialSink {
	return &SerialSink{inner: inner}
}

// Provision forwards the scheduling decision.
func (s *SerialSink) Provision(info ProvisionInfo) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.inner.Provision(info)
}

// Payload forwards a response chunk.
func (s *SerialSink) Payload(p []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.inner.Payload(p)
}

// Logs forwards a log line.
func (s *SerialSink) Logs(msg string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.inner.Logs(msg)
}
