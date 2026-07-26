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
	// IdleTimeout is the gap after which a partially received response is
	// treated as complete. It is a safety net: the executor normally closes
	// the connection, which surfaces as io.EOF.
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

// WriteChunk sends part of the request body.
func (s *Session) WriteChunk(p []byte) error {
	if len(p) == 0 {
		return nil
	}
	return s.write(p, "request chunk")
}

// CloseRequest terminates the request body.
//
// The marker is written on its own so it lands at the end of a recv boundary,
// which is what app.py's endswith check depends on.
func (s *Session) CloseRequest() error {
	return s.write(EOFMarker, "request terminator")
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

// ReadResponse streams the response, handing each chunk to sink.
//
// Termination, in the order it is checked:
//
//   - io.EOF, which is what the executor's per-request connection close
//     produces and therefore the normal path;
//   - a read deadline expiring after at least one byte arrived, treated as the
//     end of the response so an executor that holds the connection open cannot
//     wedge the request;
//   - a read deadline expiring with nothing received, reported as ErrNoResponse;
//   - ctx being cancelled.
//
// Cancellation works by pushing the connection's read deadline into the past,
// which interrupts a blocked Read. The previous implementation polled ctx with
// a non-blocking select before each read, which could never fire while a read
// was in progress — the only state that mattered.
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

	buf := make([]byte, s.opts.ChunkSize)
	received := 0

	for {
		if cancelled.Load() {
			return ctx.Err()
		}

		timeout := s.opts.IdleTimeout
		if received == 0 {
			timeout = s.opts.FirstByteTimeout
		}
		// A deadline that cannot be set is not itself a failure: the read that
		// follows reports the real condition, which for an already-closed
		// connection is the io.EOF that ends the response normally.
		if err := s.conn.SetReadDeadline(time.Now().Add(timeout)); err != nil {
			s.log.Debug("could not set read deadline", "err", err)
		}
		// Cancellation landing between the check above and the deadline just
		// set would otherwise be undone by it, re-arming a full-length read.
		if cancelled.Load() {
			_ = s.conn.SetReadDeadline(time.Now())
		}

		n, err := s.conn.Read(buf)

		// Deliver before inspecting the error: a read can return both data and
		// io.EOF, and dropping that final chunk would truncate the response.
		if n > 0 {
			received += n
			if sinkErr := sink(buf[:n]); sinkErr != nil {
				return sinkErr
			}
		}

		switch {
		case err == nil:
			continue

		case errors.Is(err, io.EOF):
			return nil

		case errors.Is(err, os.ErrDeadlineExceeded):
			// A deadline that fired because ctx ended is cancellation, not idleness.
			if cancelled.Load() {
				return ctx.Err()
			}
			if received == 0 {
				return fmt.Errorf("%w within %s", ErrNoResponse, s.opts.FirstByteTimeout)
			}
			s.log.Warn("executor left the connection open after replying; treating the response as complete",
				"idle_timeout", s.opts.IdleTimeout, "bytes", received)
			return nil

		default:
			if cancelled.Load() {
				return ctx.Err()
			}
			return fmt.Errorf("read response: %w", err)
		}
	}
}

// Close releases the connection. It is idempotent.
func (s *Session) Close() error {
	s.closeOnce.Do(func() { s.closeErr = s.conn.Close() })
	return s.closeErr
}
