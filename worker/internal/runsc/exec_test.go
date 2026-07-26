package runsc

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// These tests exercise ExecRunner against a real process, using the standard
// os/exec helper-process pattern: the test binary re-invokes itself as a stand
// in for runsc. That needs no shell script and no build step, and behaves the
// same on darwin and linux — which matters, because gVisor itself cannot run
// on a development Mac at all.

const (
	helperEnv       = "CRFS_RUNSC_HELPER"
	helperArgvFile  = "CRFS_RUNSC_HELPER_ARGV"
	helperStdout    = "CRFS_RUNSC_HELPER_STDOUT"
	helperStderr    = "CRFS_RUNSC_HELPER_STDERR"
	helperExit      = "CRFS_RUNSC_HELPER_EXIT"
	helperOrphanOut = "CRFS_RUNSC_HELPER_ORPHAN"
)

// TestHelperProcess is not a real test: it is the body of the stand-in
// executable. It returns immediately unless invoked with the helper marker.
func TestHelperProcess(t *testing.T) {
	if os.Getenv(helperEnv) != "1" {
		return
	}

	// Arguments after "--" are the ones the runner passed.
	args := os.Args
	for i, a := range args {
		if a == "--" {
			args = args[i+1:]
			break
		}
	}

	if path := os.Getenv(helperArgvFile); path != "" {
		raw, err := json.Marshal(args)
		if err == nil {
			_ = os.WriteFile(path, raw, 0o644)
		}
	}

	// Reproduce runsc's daemonise semantics: spawn a detached grandchild that
	// writes to the inherited descriptor *after* this process has exited, then
	// exit immediately.
	if path := os.Getenv(helperOrphanOut); path != "" {
		spawnOrphan(path)
	}

	if s := os.Getenv(helperStdout); s != "" {
		_, _ = os.Stdout.WriteString(s)
	}
	if s := os.Getenv(helperStderr); s != "" {
		_, _ = os.Stderr.WriteString(s)
	}

	code := 0
	if s := os.Getenv(helperExit); s != "" {
		code = atoiOr(s, 0)
	}
	os.Exit(code)
}

// spawnOrphan starts a detached process that writes to this process's stdout
// descriptor a moment from now, after this process has already exited.
func spawnOrphan(marker string) {
	cmd := exec.Command("/bin/sh", "-c", "sleep 0.4; printf 'from-the-orphan\\n' >&1")
	// Hand it the same descriptor we were given, which is the whole point.
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return
	}
	_ = os.WriteFile(marker, []byte("spawned"), 0o644)
	// Deliberately do not Wait: the grandchild must outlive us.
}

func atoiOr(s string, fallback int) int {
	n := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			return fallback
		}
		n = n*10 + int(c-'0')
	}
	return n
}

// helperRunner returns an ExecRunner that re-enters the test binary.
func helperRunner(env ...string) *ExecRunner {
	return &ExecRunner{
		binary:     os.Args[0],
		prefixArgs: []string{"-test.run=TestHelperProcess", "--"},
		env:        append([]string{helperEnv + "=1"}, env...),
	}
}

func TestExecRunner_PassesArgvThrough(t *testing.T) {
	argvFile := filepath.Join(t.TempDir(), "argv.json")
	runner := helperRunner(helperArgvFile + "=" + argvFile)

	_, err := runner.Output(context.Background(), "--root=/run/x", "--network=none", "state", "cid")
	require.NoError(t, err)

	raw, err := os.ReadFile(argvFile)
	require.NoError(t, err)

	var got []string
	require.NoError(t, json.Unmarshal(raw, &got))
	assert.Equal(t, []string{"--root=/run/x", "--network=none", "state", "cid"}, got)
}

func TestExecRunner_CapturesStdoutAndStderrSeparately(t *testing.T) {
	runner := helperRunner(
		helperStdout+"=this is stdout",
		helperStderr+"=this is stderr",
	)

	res, err := runner.Output(context.Background(), "state", "cid")
	require.NoError(t, err)

	assert.Equal(t, "this is stdout", string(res.Stdout))
	assert.Equal(t, "this is stderr", string(res.Stderr))
	assert.Zero(t, res.ExitCode)
}

// An unrecognised runtime failure must stay debuggable, which means keeping
// the exit code and stderr on the error.
func TestExecRunner_SurfacesExitCodeAndStderr(t *testing.T) {
	runner := helperRunner(
		helperStderr+"=loading spec: no such file or directory",
		helperExit+"=2",
	)

	res, err := runner.Output(context.Background(), "create", "--bundle=/nope", "cid")
	require.Error(t, err)
	assert.Equal(t, 2, res.ExitCode)

	var cmdErr *CommandError
	require.ErrorAs(t, err, &cmdErr)
	assert.Equal(t, "create", cmdErr.Command)
	assert.Equal(t, 2, cmdErr.ExitCode)
	assert.Contains(t, cmdErr.Stderr, "no such file")
	assert.Contains(t, err.Error(), "no such file", "the reason belongs in the message")
}

func TestExecRunner_MissingBinaryReportsExitCodeMinusOne(t *testing.T) {
	runner := NewExecRunner(filepath.Join(t.TempDir(), "definitely-not-runsc"))

	_, err := runner.Output(context.Background(), "state", "cid")
	require.Error(t, err)

	var cmdErr *CommandError
	require.ErrorAs(t, err, &cmdErr)
	assert.Equal(t, -1, cmdErr.ExitCode, "-1 is what marks the runtime itself as unusable")
}

func TestExecRunner_ContextCancellationIsReported(t *testing.T) {
	runner := &ExecRunner{
		binary:     "/bin/sh",
		prefixArgs: []string{"-c", "sleep 30", "--"},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()

	start := time.Now()
	_, err := runner.Output(ctx, "state")
	require.Error(t, err)
	assert.Less(t, time.Since(start), 10*time.Second)

	var cmdErr *CommandError
	require.ErrorAs(t, err, &cmdErr)
	assert.ErrorIs(t, cmdErr.Err, context.DeadlineExceeded,
		"the deadline is a more useful explanation than the kill signal it produced")
}

// This is the test the fake runner cannot substitute for. runsc daemonises the
// sandbox and exits, and the sandbox goes on writing to the inherited
// descriptor for the rest of the execution. Passing an io.Writer instead of an
// *os.File would make os/exec interpose a pipe that this process closes on
// Wait, yielding an empty log and a SIGPIPE'd executor — and every unit test
// would still pass.
func TestExecRunner_SpawnedDescriptorsOutliveTheChild(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "sandbox.log")
	marker := filepath.Join(dir, "orphan-spawned")

	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	require.NoError(t, err)

	devNull, err := os.Open(os.DevNull)
	require.NoError(t, err)

	runner := helperRunner(
		helperOrphanOut+"="+marker,
		helperStdout+"=from-the-helper\n",
	)

	_, err = runner.Spawn(context.Background(),
		Stdio{In: devNull, Out: logFile, Err: logFile},
		"create", "--bundle="+dir, "cid")
	require.NoError(t, err)

	// Release our copies, exactly as the adapter does. The orphan keeps its own.
	require.NoError(t, logFile.Close())
	require.NoError(t, devNull.Close())
	require.FileExists(t, marker, "the helper should have spawned a detached writer")

	require.Eventually(t, func() bool {
		contents, readErr := os.ReadFile(logPath)
		return readErr == nil && strings.Contains(string(contents), "from-the-orphan")
	}, 10*time.Second, 50*time.Millisecond,
		"a process outliving the runner must still reach the log file")

	raw, err := os.ReadFile(logPath)
	require.NoError(t, err)
	assert.Contains(t, string(raw), "from-the-helper", "the child's own output must land too")
}

// A failed spawn must leave its diagnostics in the sandbox log, or a create
// that dies has no trace anywhere.
func TestExecRunner_SpawnTeesStderrIntoTheLog(t *testing.T) {
	dir := t.TempDir()
	logPath := filepath.Join(dir, "sandbox.log")

	logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	require.NoError(t, err)
	defer func() { _ = logFile.Close() }()

	runner := helperRunner(helperStderr+"=creating container: permission denied", helperExit+"=1")

	_, err = runner.Spawn(context.Background(), Stdio{Out: logFile, Err: logFile}, "create", "cid")
	require.Error(t, err)

	var cmdErr *CommandError
	require.ErrorAs(t, err, &cmdErr)
	assert.Contains(t, cmdErr.Stderr, "permission denied")

	raw, err := os.ReadFile(logPath)
	require.NoError(t, err)
	assert.Contains(t, string(raw), "permission denied", "a failed create must leave a trace")
}

func TestCommandName(t *testing.T) {
	tests := []struct {
		args []string
		want string
	}{
		{[]string{"--root=/x", "--network=none", "create", "--bundle=/b", "cid"}, "create"},
		{[]string{"--root=/x", "state", "cid"}, "state"},
		{[]string{"--version"}, "--version"},
		{[]string{"--root=/x", "--version"}, "--version"},
		{[]string{"--root=/x"}, "runsc"},
		{nil, "runsc"},
	}
	for _, tt := range tests {
		assert.Equal(t, tt.want, commandName(tt.args), "%v", tt.args)
	}
}

func TestCommandError_Message(t *testing.T) {
	err := &CommandError{
		Command:  "restore",
		ExitCode: 1,
		Stderr:   "usage banner\nmore banner\nrestoring container: image not found",
		Err:      errors.New("exit status 1"),
	}

	msg := err.Error()
	assert.Contains(t, msg, "restore")
	assert.Contains(t, msg, "exit 1")
	assert.Contains(t, msg, "image not found", "the last line carries the real reason")
	assert.NotContains(t, msg, "usage banner", "the banner is noise")
}
