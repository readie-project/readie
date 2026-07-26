// Package logging constructs the structured logger used throughout the worker.
//
// Every log line carries the worker identity, and per-request loggers add
// request_id, session_id and container_id via Logger.With, so a single
// execution can be traced end to end.
package logging

import (
	"io"
	"log/slog"
	"os"
)

// Standard attribute keys. Using constants keeps them greppable and prevents
// the "requestId" / "request-id" / "request_id" drift that makes logs unqueryable.
const (
	KeyRequestID   = "request_id"
	KeySessionID   = "session_id"
	KeyContainerID = "container_id"
	KeyWorkerID    = "worker_id"
	KeyCheckpoint  = "checkpoint_id"
	KeyError       = "err"
)

// Options configures the logger.
type Options struct {
	Level  slog.Level
	Format string    // "json" (default) or "text"
	Out    io.Writer // defaults to os.Stdout
}

// New builds a logger. It never fails: an unrecognised format falls back to
// JSON, because losing logs is a worse outcome than an unexpected format.
func New(opts Options) *slog.Logger {
	out := opts.Out
	if out == nil {
		out = os.Stdout
	}

	handlerOpts := &slog.HandlerOptions{Level: opts.Level}

	var handler slog.Handler
	if opts.Format == "text" {
		handler = slog.NewTextHandler(out, handlerOpts)
	} else {
		handler = slog.NewJSONHandler(out, handlerOpts)
	}
	return slog.New(handler)
}

// Discard returns a logger that writes nothing. Useful in tests that assert on
// behaviour rather than output.
func Discard() *slog.Logger {
	return slog.New(slog.DiscardHandler)
}
