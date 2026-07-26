package runsc

import (
	"context"
	"errors"
	"strings"
	"sync"
)

// call is one recorded runsc invocation.
type call struct {
	Args  []string
	Stdio Stdio
}

// fakeRunner records argv and replays canned output.
//
// It exists to assert the exact command lines the adapter builds. runsc parses
// flags with stdlib flag semantics, so a global flag placed after the
// subcommand — or a bare `--flag value` instead of `--flag=value` — is
// silently misread; only full-slice argv equality catches that.
type fakeRunner struct {
	mu    sync.Mutex
	calls []call

	// stdout is canned output keyed by subcommand.
	stdout map[string][]byte
	// fail is an error keyed by subcommand.
	fail map[string]error
	// failOnce is consumed after firing, for testing retry paths.
	failOnce map[string]error
}

var _ Runner = (*fakeRunner)(nil)

func newFakeRunner() *fakeRunner {
	return &fakeRunner{
		stdout:   make(map[string][]byte),
		fail:     make(map[string]error),
		failOnce: make(map[string]error),
	}
}

func (f *fakeRunner) Output(_ context.Context, args ...string) (Result, error) {
	return f.record(call{Args: args})
}

func (f *fakeRunner) Spawn(_ context.Context, stdio Stdio, args ...string) (Result, error) {
	return f.record(call{Args: args, Stdio: stdio})
}

func (f *fakeRunner) record(c call) (Result, error) {
	f.mu.Lock()
	defer f.mu.Unlock()

	f.calls = append(f.calls, c)
	name := commandName(c.Args)

	if err, ok := f.failOnce[name]; ok {
		delete(f.failOnce, name)
		return Result{ExitCode: 1}, err
	}
	if err, ok := f.fail[name]; ok {
		return Result{ExitCode: 1}, err
	}
	return Result{Stdout: f.stdout[name]}, nil
}

// argv returns every recorded argument list, in order.
func (f *fakeRunner) argv() [][]string {
	f.mu.Lock()
	defer f.mu.Unlock()

	out := make([][]string, 0, len(f.calls))
	for _, c := range f.calls {
		out = append(out, c.Args)
	}
	return out
}

// commands returns the subcommand of each recorded call, in order.
func (f *fakeRunner) commands() []string {
	out := make([]string, 0)
	for _, args := range f.argv() {
		out = append(out, commandName(args))
	}
	return out
}

// lastStdio returns the descriptors passed to the most recent Spawn.
func (f *fakeRunner) lastStdio() Stdio {
	f.mu.Lock()
	defer f.mu.Unlock()

	for i := len(f.calls) - 1; i >= 0; i-- {
		if f.calls[i].Stdio.Out != nil {
			return f.calls[i].Stdio
		}
	}
	return Stdio{}
}

func (f *fakeRunner) reset() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = nil
}

// argvFor returns the recorded argv for a subcommand, or nil.
func (f *fakeRunner) argvFor(command string) []string {
	for _, args := range f.argv() {
		if commandName(args) == command {
			return args
		}
	}
	return nil
}

// commandErr builds a failure carrying runtime stderr, as ExecRunner would.
func commandErr(command, stderr string, exitCode int) error {
	return &CommandError{
		Command:  command,
		ExitCode: exitCode,
		Stderr:   stderr,
		Err:      errors.New("exit status " + strings.TrimSpace(itoa(exitCode))),
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var buf [20]byte
	pos := len(buf)
	for i > 0 {
		pos--
		buf[pos] = byte('0' + i%10)
		i /= 10
	}
	if neg {
		pos--
		buf[pos] = '-'
	}
	return string(buf[pos:])
}
