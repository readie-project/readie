// Package executor speaks the unix-socket protocol shared with the Python
// executor in executor/src/readie_executor/.
//
// # Protocol (version 2)
//
// The executor is the server: it binds $EXECUTOR_DIR/executor.sock inside its
// container, which the worker sees at <workerDir>/<containerID>/executor.sock
// through a bind mount. The worker is the client and dials that path.
//
// A message is a sequence of length-prefixed chunks ending in a zero-length
// one:
//
//	message:   ([8-byte big-endian length][chunk])* [8 zero bytes]
//
//	request:   one message, the cloudpickle of {func, args, kwargs}
//	response:  one message, the cloudpickle of a result envelope
//
// Framed per chunk rather than once per message so a sender can stream without
// knowing the total size. This side relays request bytes straight from a gRPC
// stream and learns the length only when that stream ends; a single prefix
// would force buffering an entire payload — possibly hundreds of megabytes —
// purely to count it.
//
// # The envelope
//
// The response body is a cloudpickled mapping, opaque to this package: the
// worker relays those bytes to the router without decoding them, and only the
// client SDK interprets them.
//
//	{"ok": true,  "value": <return value>}
//	{"ok": false, "exc_type": str, "message": str, "traceback": str}
//
// A false "ok" is *not* a worker failure. The sandbox ran, the interpreter is
// healthy, and the container is still reusable — so the execution is reported
// as a success and the container is paused for reuse. Only the client turns
// that envelope into an exception.
//
// # What version 1 could not express
//
// Version 1 ended a request with the unframed literal bytes "EOF" and a
// response by closing the connection. A body could not contain its own
// terminator; a response truncated by a dying executor was indistinguishable
// from a short one; and a raising function sent nothing at all, so failure had
// to be inferred from an empty response. All three are gone.
//
// # Versioning
//
// The generation manifest records executor_protocol. A worker refuses a
// generation whose version it does not implement rather than dialing an
// executor that will never send a terminator it recognises.
//
// # Cross-language agreement
//
// executor/tests/data/frames.golden.json is decoded by both this package's
// tests and the Python suite. Two hand-written implementations of one wire
// format drift; the shared fixture makes that a test failure.
package executor

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"time"
)

// ProtocolVersion is the wire format this package implements.
const ProtocolVersion = 2

// LengthBytes is the size of a chunk's length prefix.
//
// Big-endian because it is the wire convention and reads correctly in a hex
// dump; 64-bit because a payload can legitimately be a multi-gigabyte array.
const LengthBytes = 8

// DefaultChunkSize matches DEFAULT_CHUNK_SIZE in the Python executor's config.
const DefaultChunkSize = 1024 * 1024

// MaxChunkBytes bounds a single chunk.
//
// The prefix arrives from the peer, so an implausible one must be rejected
// before it is used as an allocation size.
const MaxChunkBytes = 64 * 1024 * 1024

// MaxMessageBytes bounds a whole message however it is chunked. The per-chunk
// cap alone does not stop a peer sending small chunks forever.
const MaxMessageBytes = 4 * 1024 * 1024 * 1024

// Sentinel errors returned by this package.
var (
	// ErrDialTimeout indicates the executor's socket never became connectable.
	ErrDialTimeout = errors.New("executor socket did not accept connections in time")
	// ErrNoResponse indicates the executor accepted the request but produced
	// nothing before the first-byte deadline.
	ErrNoResponse = errors.New("executor produced no response")
	// ErrShortWrite indicates the socket accepted fewer bytes than offered.
	ErrShortWrite = errors.New("short write to executor socket")
	// ErrTruncatedResponse indicates the stream ended before the terminating
	// zero-length chunk.
	//
	// Version 1 could not detect this at all: the response ended at connection
	// close, so an executor killed mid-write was reported as a success with a
	// short body, and the client unpickled a truncated payload.
	ErrTruncatedResponse = errors.New("executor response ended before its terminator")
	// ErrMalformedFrame indicates a length prefix this side refuses to honour.
	ErrMalformedFrame = errors.New("malformed executor frame")
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

// EncodeLength renders a chunk length prefix.
func EncodeLength(n int) []byte {
	buf := make([]byte, LengthBytes)
	binary.BigEndian.PutUint64(buf, uint64(n))
	return buf
}

// DecodeLength reads a chunk length prefix.
func DecodeLength(buf []byte) (int, error) {
	if len(buf) != LengthBytes {
		return 0, fmt.Errorf("%w: length prefix must be %d bytes, got %d",
			ErrMalformedFrame, LengthBytes, len(buf))
	}

	n := binary.BigEndian.Uint64(buf)
	// Compared as uint64 before the int conversion: on a 32-bit build the
	// conversion would silently truncate, and a bounds check afterwards would
	// be checking the wrong number.
	if n > MaxChunkBytes {
		return 0, fmt.Errorf("%w: chunk claims %d bytes, over the %d-byte limit",
			ErrMalformedFrame, n, MaxChunkBytes)
	}
	return int(n), nil
}

// Terminator is the zero-length chunk that ends a message.
func Terminator() []byte { return EncodeLength(0) }
