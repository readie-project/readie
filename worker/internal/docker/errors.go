package docker

import (
	"errors"
	"fmt"

	"github.com/containerd/errdefs"
)

// Error kinds. Callers match on these with errors.Is rather than on daemon
// error strings, which are not stable.
var (
	// ErrNotFound indicates the container does not exist.
	ErrNotFound = errors.New("container not found")
	// ErrConflict indicates the operation conflicts with the container's state,
	// for example unpausing a container that is not paused.
	ErrConflict = errors.New("container state conflict")
	// ErrDaemonUnavailable indicates the container runtime could not be reached.
	ErrDaemonUnavailable = errors.New("container runtime unavailable")
	// ErrInvalidSpec indicates the runtime rejected the requested configuration.
	ErrInvalidSpec = errors.New("invalid container spec")
)

// Error is the error type returned by every Port method. It carries both a
// kind, for errors.Is matching, and the underlying runtime error, for logs.
type Error struct {
	Op   string // the Port operation, for example "create" or "start"
	ID   string // container id, empty where not applicable
	Kind error  // one of the sentinels above, or nil when unclassified
	Err  error  // the underlying runtime error
}

func (e *Error) Error() string {
	switch {
	case e.ID == "" && e.Kind == nil:
		return fmt.Sprintf("docker %s: %v", e.Op, e.Err)
	case e.ID == "":
		return fmt.Sprintf("docker %s: %v: %v", e.Op, e.Kind, e.Err)
	case e.Kind == nil:
		return fmt.Sprintf("docker %s %s: %v", e.Op, e.ID, e.Err)
	default:
		return fmt.Sprintf("docker %s %s: %v: %v", e.Op, e.ID, e.Kind, e.Err)
	}
}

// Unwrap exposes both the kind and the cause, so errors.Is matches the
// sentinel and callers that need the raw runtime error can still reach it.
func (e *Error) Unwrap() []error {
	if e.Kind == nil {
		return []error{e.Err}
	}
	return []error{e.Kind, e.Err}
}

// wrap classifies a runtime error and attaches operation context. It returns
// nil for a nil error so call sites can wrap unconditionally.
func wrap(op, id string, err error) error {
	if err == nil {
		return nil
	}
	return &Error{Op: op, ID: id, Kind: classify(err), Err: err}
}

// classify maps a runtime error onto one of this package's kinds. The moby
// client annotates its errors with containerd/errdefs, so the mapping does not
// depend on message text.
func classify(err error) error {
	switch {
	case errdefs.IsNotFound(err):
		return ErrNotFound
	case errdefs.IsConflict(err), errdefs.IsAlreadyExists(err), errdefs.IsFailedPrecondition(err):
		return ErrConflict
	case errdefs.IsUnavailable(err):
		return ErrDaemonUnavailable
	case errdefs.IsInvalidArgument(err):
		return ErrInvalidSpec
	default:
		return nil
	}
}
