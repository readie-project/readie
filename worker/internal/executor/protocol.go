// Package executor speaks the unix-socket protocol shared with the Python
// executor in scripts/executor/app.py.
//
// # Protocol
//
// The executor is the server: it binds $EXECUTOR_DIR/executor.sock inside its
// container, which the worker sees at <workerDir>/<containerID>/executor.sock
// through a bind mount. The worker is the client and dials that path.
//
// A request is the raw cloudpickle encoding of {'func', 'args', 'kwargs'},
// written in chunks and terminated by the literal bytes "EOF" sent as their own
// write. The response is the cloudpickle encoding of the return value, streamed
// back in chunks, after which the executor closes the connection and accepts
// the next one.
//
// # Known hazards
//
// The terminator is unframed. app.py detects it with data.endswith(b"EOF") and
// then strips it with data.rstrip(b"EOF"), which removes any trailing run of
// the bytes 'E', 'O' and 'F' rather than the literal suffix. A payload whose
// final bytes fall in that set is therefore silently truncated. This cannot be
// fixed from the Go side without changing the protocol on both ends; a
// length-prefixed framing would be the correct replacement.
//
// The executor also sleeps 30 seconds awaiting a checkpoint before it binds its
// socket, so any dial budget at or below that can never succeed against a
// container that started cold rather than being restored.
package executor

import (
	"errors"
	"io"
	"time"
)

// EOFMarker terminates a request body. See the package documentation for the
// truncation hazard this framing carries.
var EOFMarker = []byte("EOF")

// DefaultChunkSize matches CHUNK_SIZE in scripts/executor/app.py.
const DefaultChunkSize = 1024 * 1024

// Sentinel errors returned by this package.
var (
	// ErrDialTimeout indicates the executor's socket never became connectable.
	ErrDialTimeout = errors.New("executor socket did not accept connections in time")
	// ErrNoResponse indicates the executor accepted the request but produced
	// nothing before the first-byte deadline.
	ErrNoResponse = errors.New("executor produced no response")
	// ErrShortWrite indicates the socket accepted fewer bytes than offered.
	ErrShortWrite = errors.New("short write to executor socket")
)

// Conn is the subset of net.Conn a Session needs.
//
// Keeping it narrow means net.Pipe satisfies it, so the orchestration layer can
// be tested without touching the filesystem.
type Conn interface {
	io.ReadWriteCloser
	SetReadDeadline(t time.Time) error
	SetWriteDeadline(t time.Time) error
}
