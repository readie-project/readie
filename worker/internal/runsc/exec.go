// Package runsc implements sandbox.Port by driving the runsc(1) CLI.
//
// gVisor has no daemon and no API: every operation is a process invocation, so
// this package is where argv construction, output parsing and process
// lifetimes live. It is the only package that knows gVisor exists.
//
// # Flag placement
//
// runsc parses flags with google/subcommands over stdlib flag, which stops at
// the first non-flag argument. Global flags must therefore precede the
// subcommand, subcommand flags must follow it and precede the container id,
// and every flag must use the --flag=value form. Getting this wrong produces
// confusing "unknown command" errors rather than a clean rejection, which is
// why argv is asserted with full-slice equality in the tests.
package runsc

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

// versionFlag queries the runtime version. It is the one invocation with no
// subcommand.
const versionFlag = "--version"

// Stdio are the file descriptors handed to a spawned process.
//
// The fields are *os.File and not io.Writer on purpose. os/exec passes an
// *os.File to the child as a direct descriptor, but wraps anything else in an
// os.Pipe whose write end this process owns and closes when Wait returns.
// `runsc create` exits immediately while the sandbox it spawned keeps writing
// for the lifetime of the execution, so a pipe would yield an empty log and a
// SIGPIPE'd executor the moment the parent reaped the child.
type Stdio struct {
	In  *os.File
	Out *os.File
	Err *os.File
}

// Result is the outcome of a completed runsc invocation.
type Result struct {
	Stdout   []byte
	Stderr   []byte
	ExitCode int
}

// Runner executes runsc commands. It exists so the adapter's argv construction
// and output parsing can be tested without a runsc binary.
type Runner interface {
	// Output runs a command to completion and captures its output.
	Output(ctx context.Context, args ...string) (Result, error)
	// Spawn runs a command to completion with the given descriptors attached.
	//
	// It is distinct from Output because the descriptors must outlive this
	// process's child: runsc daemonises the sandbox, which inherits them.
	Spawn(ctx context.Context, stdio Stdio, args ...string) (Result, error)
}

// ExecRunner runs a real runsc binary.
type ExecRunner struct {
	// binary is the executable to run.
	binary string
	// prefixArgs precede every command's arguments. Used only by tests, to
	// re-enter the test binary as a stand-in for runsc.
	prefixArgs []string
	// env, when non-empty, replaces the child's environment.
	env []string
}

var _ Runner = (*ExecRunner)(nil)

// NewExecRunner returns a Runner invoking the given runsc binary.
func NewExecRunner(binary string) *ExecRunner {
	return &ExecRunner{binary: binary}
}

// Output runs a command and captures stdout and stderr separately.
func (r *ExecRunner) Output(ctx context.Context, args ...string) (Result, error) {
	var stdout, stderr bytes.Buffer

	cmd := r.command(ctx, args...)
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err := cmd.Run()
	result := Result{
		Stdout:   stdout.Bytes(),
		Stderr:   stderr.Bytes(),
		ExitCode: cmd.ProcessState.ExitCode(),
	}
	return result, r.classify(ctx, args, result, err)
}

// Spawn runs a command with stdio attached, capturing nothing.
//
// Descriptors passed here are inherited by whatever runsc daemonises, so they
// remain valid after this call returns.
func (r *ExecRunner) Spawn(ctx context.Context, stdio Stdio, args ...string) (Result, error) {
	var stderr bytes.Buffer

	cmd := r.command(ctx, args...)
	cmd.Stdin = stdio.In
	cmd.Stdout = stdio.Out
	// runsc writes its own diagnostics to stderr. Tee them into the sandbox
	// log so a failed create leaves a trace, while still capturing them for
	// the error message.
	if stdio.Err != nil {
		cmd.Stderr = io.MultiWriter(stdio.Err, &stderr)
	} else {
		cmd.Stderr = &stderr
	}

	err := cmd.Run()
	result := Result{
		Stderr:   stderr.Bytes(),
		ExitCode: cmd.ProcessState.ExitCode(),
	}
	return result, r.classify(ctx, args, result, err)
}

func (r *ExecRunner) command(ctx context.Context, args ...string) *exec.Cmd {
	full := make([]string, 0, len(r.prefixArgs)+len(args))
	full = append(full, r.prefixArgs...)
	full = append(full, args...)

	cmd := exec.CommandContext(ctx, r.binary, full...)
	if len(r.env) > 0 {
		cmd.Env = r.env
	}
	return cmd
}

// classify turns a process failure into an error carrying the exit code and
// stderr, which is what makes an unrecognised runsc failure debuggable.
func (r *ExecRunner) classify(ctx context.Context, args []string, result Result, err error) error {
	if err == nil {
		return nil
	}

	// A cancelled or timed-out context is the more useful explanation than the
	// kill signal it produced.
	if ctxErr := ctx.Err(); ctxErr != nil {
		return &CommandError{
			Command: commandName(args), Args: args,
			ExitCode: result.ExitCode, Stderr: string(result.Stderr), Err: ctxErr,
		}
	}

	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return &CommandError{
			Command: commandName(args), Args: args,
			ExitCode: result.ExitCode, Stderr: string(result.Stderr), Err: err,
		}
	}

	// Not an exit status: the binary is missing or not executable.
	return &CommandError{
		Command: commandName(args), Args: args,
		ExitCode: -1, Stderr: string(result.Stderr), Err: err,
	}
}

// commandName returns the subcommand from an argv, for error messages and for
// keying test doubles. It is the first argument that is not a global flag,
// except that a bare version query names itself - there is no subcommand, and
// "runsc runsc failed" would read poorly.
func commandName(args []string) string {
	for _, a := range args {
		if a == versionFlag {
			return versionFlag
		}
		if !strings.HasPrefix(a, "-") {
			return a
		}
	}
	return "runsc"
}

// CommandError is a failed runsc invocation.
type CommandError struct {
	Command  string
	Args     []string
	ExitCode int
	Stderr   string
	Err      error
}

func (e *CommandError) Error() string {
	msg := fmt.Sprintf("runsc %s failed (exit %d)", e.Command, e.ExitCode)
	if trimmed := strings.TrimSpace(e.Stderr); trimmed != "" {
		msg += ": " + lastLine(trimmed)
	}
	if e.Err != nil {
		msg += ": " + e.Err.Error()
	}
	return msg
}

func (e *CommandError) Unwrap() error { return e.Err }

// lastLine returns the final non-empty line, which is where runsc puts the
// actual reason; everything before it is usually a usage banner.
func lastLine(s string) string {
	lines := strings.Split(s, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if line := strings.TrimSpace(lines[i]); line != "" {
			return line
		}
	}
	return s
}
