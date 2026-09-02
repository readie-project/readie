package sandbox

import (
	"errors"
	"fmt"
	"strings"
)

// Error kinds. Callers match on these with errors.Is rather than on runtime
// error strings, which are not stable.
var (
	// ErrNotFound indicates the sandbox does not exist.
	ErrNotFound = errors.New("sandbox not found")
	// ErrConflict indicates the operation conflicts with the sandbox's state,
	// for example unpausing one that is not paused or creating one twice.
	ErrConflict = errors.New("sandbox state conflict")
	// ErrRuntimeUnavailable indicates the sandbox runtime could not be used at
	// all - a missing binary, an unusable state directory, a failed probe.
	ErrRuntimeUnavailable = errors.New("sandbox runtime unavailable")
	// ErrInvalidSpec indicates the requested configuration is unusable.
	ErrInvalidSpec = errors.New("invalid sandbox spec")
	// ErrRestoreFailed indicates a checkpoint could not be restored. It is the
	// one error with a policy attached: the caller downgrades to a cold start.
	ErrRestoreFailed = errors.New("checkpoint restore failed")
	// ErrCheckpointFailed indicates a snapshot could not be taken.
	ErrCheckpointFailed = errors.New("checkpoint failed")
	// ErrUnsupported indicates the runtime cannot perform the operation.
	ErrUnsupported = errors.New("operation unsupported by the runtime")
)

// Error is the error type returned by Port methods. It carries a kind for
// errors.Is matching plus the runtime's own diagnostics, so an unrecognised
// failure is still debuggable.
type Error struct {
	// Op is the Port operation, for example "create" or "start".
	Op string
	// ID is the sandbox id, empty where not applicable.
	ID string
	// Kind is one of the sentinels above, or nil when unclassified.
	Kind error
	// Err is the underlying cause.
	Err error
}

func (e *Error) Error() string {
	var b strings.Builder

	b.WriteString("sandbox ")
	b.WriteString(e.Op)
	if e.ID != "" {
		b.WriteString(" ")
		b.WriteString(e.ID)
	}
	if e.Kind != nil {
		b.WriteString(": ")
		b.WriteString(e.Kind.Error())
	}
	if e.Err != nil {
		b.WriteString(": ")
		b.WriteString(e.Err.Error())
	}
	return b.String()
}

// Unwrap exposes both the kind and the cause, so errors.Is matches the
// sentinel while callers needing the raw failure can still reach it.
func (e *Error) Unwrap() []error {
	if e.Kind == nil {
		return []error{e.Err}
	}
	return []error{e.Kind, e.Err}
}

// Wrap attaches operation context and a kind to a runtime failure. It returns
// nil for a nil error so call sites can wrap unconditionally.
func Wrap(op, id string, kind, err error) error {
	if err == nil {
		return nil
	}
	return &Error{Op: op, ID: id, Kind: kind, Err: err}
}

// Errorf builds a kinded error without an underlying cause.
func Errorf(op, id string, kind error, format string, args ...any) error {
	return &Error{Op: op, ID: id, Kind: kind, Err: fmt.Errorf(format, args...)}
}
