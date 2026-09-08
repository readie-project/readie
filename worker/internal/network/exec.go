package network

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

// Result is the outcome of a completed command.
type Result struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

// CommandRunner executes ip(8)/iptables(8)/sysctl(8) commands.
//
// Neither has a daemon or a client library worth taking a dependency on - they
// are themselves thin wrappers over netlink - so this mirrors runsc.Runner's
// own rationale ("every operation is a process invocation") rather than
// introducing a netlink library this repository has never needed before.
type CommandRunner interface {
	Output(ctx context.Context, binary string, args ...string) (Result, error)
}

// ExecRunner runs real binaries.
type ExecRunner struct{}

var _ CommandRunner = ExecRunner{}

// Output runs binary with args and captures its output.
func (ExecRunner) Output(ctx context.Context, binary string, args ...string) (Result, error) {
	var stdout, stderr bytes.Buffer

	cmd := exec.CommandContext(ctx, binary, args...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	result := Result{Stdout: stdout.String(), Stderr: stderr.String()}
	if cmd.ProcessState != nil {
		result.ExitCode = cmd.ProcessState.ExitCode()
	}
	if err == nil {
		return result, nil
	}

	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return result, &CommandError{Binary: binary, Args: args, ExitCode: result.ExitCode, Stderr: result.Stderr, Err: err}
	}
	// Not an exit status: the binary is missing or not executable.
	return result, &CommandError{Binary: binary, Args: args, ExitCode: -1, Stderr: result.Stderr, Err: err}
}

// CommandError is a failed ip/iptables/sysctl invocation.
type CommandError struct {
	Binary   string
	Args     []string
	ExitCode int
	Stderr   string
	Err      error
}

func (e *CommandError) Error() string {
	msg := fmt.Sprintf("%s %s failed (exit %d)", e.Binary, strings.Join(e.Args, " "), e.ExitCode)
	if trimmed := strings.TrimSpace(e.Stderr); trimmed != "" {
		msg += ": " + trimmed
	}
	return msg
}

func (e *CommandError) Unwrap() error { return e.Err }

// looksAlreadyGone reports whether err is the shape ip(8) produces for "the
// namespace/link/rule I was asked to remove does not exist" - the noisy,
// string-matched analogue of sandbox.ErrNotFound, which ip(8) has no
// structured equivalent of.
func looksAlreadyGone(err error) bool {
	var cmdErr *CommandError
	if !errors.As(err, &cmdErr) {
		return false
	}
	stderr := strings.ToLower(cmdErr.Stderr)
	return strings.Contains(stderr, "no such file or directory") ||
		strings.Contains(stderr, "cannot find device") ||
		strings.Contains(stderr, "does not exist")
}
