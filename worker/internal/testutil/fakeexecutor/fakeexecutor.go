// Package fakeexecutor is a Go stand-in for the Python executor in ../../../../executor/.
//
// It reproduces the real executor's socket behaviour: bind a unix socket,
// accept one connection at a time, read a length-prefixed message, reply with
// another, then close and accept again. The behavioural modes let tests
// reproduce the executor's awkward edges - the 30-second pre-bind sleep, an
// executor that stalls, and one that dies part-way through a reply.
package fakeexecutor

import (
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/illinoisdata/readie/worker/internal/executor"
)

// Mode selects how the fake behaves once connected.
type Mode int

const (
	// ModeCloseAfterResponse mirrors app.py: reply, then close the connection.
	ModeCloseAfterResponse Mode = iota
	// ModeHoldOpen replies but leaves the connection open, so the reader must
	// fall back to its idle timeout.
	ModeHoldOpen
	// ModeSlowResponse waits ResponseDelay before replying.
	ModeSlowResponse
	// ModeNoResponse accepts the request and never replies.
	ModeNoResponse
	// ModeCloseImmediately closes as soon as the request arrives.
	ModeCloseImmediately
	// ModeTruncatedResponse writes a chunk header promising more than it
	// sends, then closes - an executor killed mid-reply. Version 1 could not
	// distinguish this from a complete short response.
	ModeTruncatedResponse
)

// Options configures a Server.
type Options struct {
	Mode Mode
	// Reply is the response body. Defaults to a short marker.
	Reply []byte
	// ChunkSize is the reply write granularity. Defaults to 1 MiB.
	ChunkSize int
	// BindDelay defers binding the socket, reproducing the real executor's
	// 30-second wait for a checkpoint before it starts listening.
	BindDelay time.Duration
	// ResponseDelay is the pause before replying in ModeSlowResponse.
	ResponseDelay time.Duration
}

// Server is a fake executor listening on a unix socket.
type Server struct {
	opts Options
	path string

	mu       sync.Mutex
	listener net.Listener
	requests [][]byte
	closed   bool

	started  chan struct{}
	stop     chan struct{} // closed by Close to unblock handlers and the accept loop
	stopOnce sync.Once
	exited   chan struct{} // closed by run when the accept loop has returned
}

// Start binds a fake executor at path and serves until the test ends.
//
// The socket path matters: unix sockets are limited to 104 bytes on darwin, so
// tests should root their worker directory somewhere short (see ShortTempDir)
// rather than using t.TempDir directly.
func Start(t *testing.T, path string, opts Options) *Server {
	t.Helper()

	if opts.Reply == nil {
		opts.Reply = []byte("fake-executor-reply")
	}
	if opts.ChunkSize <= 0 {
		opts.ChunkSize = 1024 * 1024
	}

	s := &Server{
		opts:    opts,
		path:    path,
		started: make(chan struct{}),
		stop:    make(chan struct{}),
		exited:  make(chan struct{}),
	}

	go s.run(t)
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func (s *Server) run(t *testing.T) {
	defer close(s.exited)

	if s.opts.BindDelay > 0 {
		select {
		case <-time.After(s.opts.BindDelay):
		case <-s.stop:
			return
		}
	}

	if err := os.MkdirAll(filepath.Dir(s.path), 0o777); err != nil {
		t.Errorf("fakeexecutor: create socket directory: %v", err)
		return
	}
	// The real executor unlinks a stale socket before binding.
	_ = os.Remove(s.path)

	listener, err := net.Listen("unix", s.path)
	if err != nil {
		// A closed server racing with startup is expected during cleanup.
		if !s.isClosed() {
			t.Errorf("fakeexecutor: listen on %s: %v", s.path, err)
		}
		return
	}

	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		_ = listener.Close()
		return
	}
	s.listener = listener
	s.mu.Unlock()
	close(s.started)

	for {
		conn, err := listener.Accept()
		if err != nil {
			return // the listener was closed
		}
		s.handle(conn)
	}
}

// handle serves one connection, mirroring app.py's per-request loop.
func (s *Server) handle(conn net.Conn) {
	defer func() { _ = conn.Close() }()

	body, err := s.readRequest(conn)
	if err != nil && !errors.Is(err, io.EOF) {
		return
	}

	s.mu.Lock()
	s.requests = append(s.requests, body)
	s.mu.Unlock()

	switch s.opts.Mode {
	case ModeCloseImmediately:
		return

	case ModeNoResponse:
		<-s.stop
		return

	case ModeSlowResponse:
		select {
		case <-time.After(s.opts.ResponseDelay):
		case <-s.stop:
			return
		}
	}

	if s.opts.Mode == ModeTruncatedResponse {
		// A header claiming more than follows, then silence.
		_, _ = conn.Write(executor.EncodeLength(len(s.opts.Reply) + 1024))
		_, _ = conn.Write(s.opts.Reply)
		return
	}

	for offset := 0; offset < len(s.opts.Reply); offset += s.opts.ChunkSize {
		end := min(offset+s.opts.ChunkSize, len(s.opts.Reply))
		piece := s.opts.Reply[offset:end]
		if _, err := conn.Write(append(executor.EncodeLength(len(piece)), piece...)); err != nil {
			return
		}
	}
	if _, err := conn.Write(executor.Terminator()); err != nil {
		return
	}

	if s.opts.Mode == ModeHoldOpen {
		// Keep the connection open so the reader must rely on its idle timeout.
		<-s.stop
	}
}

// readRequest reads length-prefixed chunks until the zero-length terminator,
// exactly as the Python executor does.
func (s *Server) readRequest(conn net.Conn) ([]byte, error) {
	var (
		body   []byte
		header = make([]byte, executor.LengthBytes)
	)

	for {
		if _, err := io.ReadFull(conn, header); err != nil {
			return body, err
		}
		length, err := executor.DecodeLength(header)
		if err != nil {
			return body, err
		}
		if length == 0 {
			return body, nil
		}

		chunk := make([]byte, length)
		if _, err := io.ReadFull(conn, chunk); err != nil {
			return body, err
		}
		body = append(body, chunk...)
	}
}

// WaitUntilListening blocks until the socket is bound or timeout elapses.
func (s *Server) WaitUntilListening(timeout time.Duration) bool {
	select {
	case <-s.started:
		return true
	case <-time.After(timeout):
		return false
	}
}

// Requests returns the request bodies received, unframed.
func (s *Server) Requests() [][]byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([][]byte(nil), s.requests...)
}

// LastRequest returns the most recent request body.
func (s *Server) LastRequest() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.requests) == 0 {
		return nil
	}
	return s.requests[len(s.requests)-1]
}

func (s *Server) isClosed() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closed
}

// Close stops the server and removes its socket.
func (s *Server) Close() error {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil
	}
	s.closed = true
	listener := s.listener
	s.mu.Unlock()

	// Unblocks handlers parked on <-s.stop as well as the accept loop.
	s.stopOnce.Do(func() { close(s.stop) })

	if listener != nil {
		_ = listener.Close()
	}
	_ = os.Remove(s.path)
	return nil
}

// ShortTempDir returns a temporary directory with a path short enough to hold
// a unix socket.
//
// Neither t.TempDir nor os.MkdirTemp("") will do. Both honour TMPDIR, which on
// darwin points at a ~60-character path under /var/folders; appending a
// container name and "/executor.sock" then exceeds the 104-byte sun_path limit
// and bind fails with the unhelpful "invalid argument". Rooting at /tmp keeps
// the whole path comfortably inside the limit.
func ShortTempDir(t *testing.T) string {
	t.Helper()

	root := "/tmp"
	if runtime.GOOS == "windows" {
		root = ""
	}

	dir, err := os.MkdirTemp(root, "wk")
	if err != nil {
		t.Fatalf("create short temp dir: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}
