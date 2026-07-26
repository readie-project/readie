package executor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// SessionOptions tunes a Session's framing and deadlines.
type SessionOptions struct {
	// ChunkSize is the response read granularity.
	ChunkSize int
	// WriteTimeout bounds a single write to the executor.
	WriteTimeout time.Duration
	// FirstByteTimeout is how long the executor may take to begin replying.
	// This spans the user's function execution, so it is generous.
	FirstByteTimeout time.Duration
	// IdleTimeout bounds the gap between two reads once a response has begun.
	//
	// Under version 1 an expired idle timeout meant "assume the response is
	// complete", because nothing on the wire said where it ended. Framing says
	// so explicitly, and this is now what it sounds like: a stalled peer.
	IdleTimeout time.Duration
}

func (o SessionOptions) withDefaults() SessionOptions {
	if o.ChunkSize <= 0 {
		o.ChunkSize = DefaultChunkSize
	}
	if o.WriteTimeout <= 0 {
		o.WriteTimeout = 30 * time.Second
	}
	if o.FirstByteTimeout <= 0 {
		o.FirstByteTimeout = time.Hour
	}
	if o.IdleTimeout <= 0 {
		o.IdleTimeout = 30 * time.Second
	}
	return o
}

// Session carries one execution over one executor connection.
//
// It is not safe for arbitrary concurrent use, but a single writer goroutine
// and a single reader goroutine may run at once, which is what net.Conn
// guarantees and what the execution runner relies on.
type Session struct {
	conn Conn
	opts SessionOptions
	log  *slog.Logger

	closeOnce sync.Once
	closeErr  error
}

// NewSession wraps a connection in the executor framing protocol.
func NewSession(conn Conn, opts SessionOptions, log *slog.Logger) *Session {
	if log == nil {
		log = slog.Default()
	}
	return &Session{conn: conn, opts: opts.withDefaults(), log: log}
}

// WriteChunk sends part of the request body as one length-prefixed chunk.
//
// An empty chunk is dropped rather than written: a zero length is the message
// terminator, so forwarding an empty relay chunk would end the request early.
func (s *Session) WriteChunk(p []byte) error {
	if len(p) == 0 {
		return nil
	}

	// Prefix and body in one write. Two writes are two packets on some
	// transports, and there is no reason to wake the reader twice per chunk.
	framed := make([]byte, 0, LengthBytes+len(p))
	framed = append(framed, EncodeLength(len(p))...)
	framed = append(framed, p...)

	return s.write(framed, "request chunk")
}

// CloseRequest terminates the request body with a zero-length chunk.
func (s *Session) CloseRequest() error {
	return s.write(Terminator(), "request terminator")
}

func (s *Session) write(p []byte, what string) error {
	// As with reads, a deadline that cannot be set is not the failure worth
	// reporting; the write below surfaces the actual problem.
	if err := s.conn.SetWriteDeadline(time.Now().Add(s.opts.WriteTimeout)); err != nil {
		s.log.Debug("could not set write deadline", "err", err)
	}

	n, err := s.conn.Write(p)
	if err != nil {
		return fmt.Errorf("write %s: %w", what, err)
	}
	if n != len(p) {
		return fmt.Errorf("write %s: %w: wrote %d of %d bytes", what, ErrShortWrite, n, len(p))
	}
	return nil
}

// ReadResponse streams the response body, handing each chunk to sink.
//
// The body is read frame by frame until the terminating zero-length chunk. The
// framing is what lets this be exact: version 1 ended at connection close, so a
// short read was indistinguishable from a complete small response and an
// executor killed mid-write was reported as a success.
//
// Cancellation works by pushing the connection's read deadline into the past,
// which interrupts a blocked Read. Polling ctx between reads could never fire
// while a read was in progress — the only state that mattered.
func (s *Session) ReadResponse(ctx context.Context, sink func([]byte) error) error {
	// cancelled is set before the hook expires the deadline, so the loop below
	// can tell "the deadline fired because we were cancelled" from "the
	// executor went quiet" without consulting ctx, and — critically — can
	// avoid pushing the deadline back out again after cancellation.
	var cancelled atomic.Bool

	// AfterFunc runs immediately when ctx is already done, so an
	// already-cancelled caller never performs a blocking read.
	stop := context.AfterFunc(ctx, func() {
		cancelled.Store(true)
		_ = s.conn.SetReadDeadline(time.Now())
	})
	defer stop()

	r := &frameReader{session: s, cancelled: &cancelled}
	buf := make([]byte, s.opts.ChunkSize)
	header := make([]byte, LengthBytes)

	for {
		if err := r.readFull(ctx, header); err != nil {
			return err
		}

		length, err := DecodeLength(header)
		if err != nil {
			return err
		}
		if length == 0 {
			return nil
		}
		if r.total+length > MaxMessageBytes {
			return fmt.Errorf("%w: response exceeded %d bytes", ErrMalformedFrame, MaxMessageBytes)
		}

		// Streamed to the sink as it arrives rather than assembled first: the
		// runner forwards these to the router, and buffering a whole response
		// here would hold every in-flight result in worker memory.
		for remaining := length; remaining > 0; {
			want := min(remaining, len(buf))
			n, err := r.read(ctx, buf[:want])
			if n > 0 {
				remaining -= n
				if sinkErr := sink(buf[:n]); sinkErr != nil {
					return sinkErr
				}
			}
			if err != nil {
				return err
			}
		}
	}
}

// frameReader applies this session's deadlines and cancellation to each read,
// and translates a stream that ends early into ErrTruncatedResponse.
//
// It holds no context: ctx is passed to each call, so a reader cannot outlive
// the cancellation it was built with.
type frameReader struct {
	session   *Session
	cancelled *atomic.Bool
	total     int
}

// readFull fills p exactly, or fails.
func (r *frameReader) readFull(ctx context.Context, p []byte) error {
	for filled := 0; filled < len(p); {
		n, err := r.read(ctx, p[filled:])
		filled += n
		if err != nil {
			return err
		}
	}
	return nil
}

// read performs one deadline-bounded read.
func (r *frameReader) read(ctx context.Context, p []byte) (int, error) {
	s := r.session

	if r.cancelled.Load() {
		return 0, ctx.Err()
	}

	// The executor spends the first-byte budget running the user's function,
	// so it is generous; once bytes are flowing a gap means a stalled peer.
	timeout := s.opts.IdleTimeout
	if r.total == 0 {
		timeout = s.opts.FirstByteTimeout
	}
	// A deadline that cannot be set is not itself a failure: the read that
	// follows reports the real condition.
	if err := s.conn.SetReadDeadline(time.Now().Add(timeout)); err != nil {
		s.log.Debug("could not set read deadline", "err", err)
	}
	// Cancellation landing between the check above and the deadline just set
	// would otherwise be undone by it, re-arming a full-length read.
	if r.cancelled.Load() {
		_ = s.conn.SetReadDeadline(time.Now())
	}

	n, err := s.conn.Read(p)
	r.total += n

	switch {
	case err == nil:
		return n, nil

	case errors.Is(err, io.EOF):
		// The peer closed. With framing that is only legitimate after the
		// terminator, and reaching here means we were still expecting bytes.
		if r.total == 0 {
			return n, fmt.Errorf("%w: connection closed before any output", ErrNoResponse)
		}
		return n, fmt.Errorf("%w after %d bytes", ErrTruncatedResponse, r.total)

	case errors.Is(err, os.ErrDeadlineExceeded):
		// A deadline that fired because ctx ended is cancellation, not idleness.
		if r.cancelled.Load() {
			return n, ctx.Err()
		}
		if r.total == 0 {
			return n, fmt.Errorf("%w within %s", ErrNoResponse, s.opts.FirstByteTimeout)
		}
		return n, fmt.Errorf("%w: executor stalled for %s after %d bytes",
			ErrTruncatedResponse, s.opts.IdleTimeout, r.total)

	default:
		if r.cancelled.Load() {
			return n, ctx.Err()
		}
		return n, fmt.Errorf("read response: %w", err)
	}
}

// Close releases the connection. It is idempotent.
func (s *Session) Close() error {
	s.closeOnce.Do(func() { s.closeErr = s.conn.Close() })
	return s.closeErr
}
