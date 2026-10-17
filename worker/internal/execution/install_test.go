package execution_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/readie-project/readie/worker/internal/container"
	"github.com/readie-project/readie/worker/internal/execution"
	"github.com/readie-project/readie/worker/internal/sandbox"
)

func withPackages(specs ...string) execution.Request {
	req := request()
	req.Packages = specs
	return req
}

func TestRun_InstallsThePackagesBeforeTheExecutorSeesTheRequest(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.execResult = sandbox.ExecResult{Output: []byte("Installed 1 package\n")}
	})

	_, err := h.runner.Run(context.Background(), withPackages("flask", "numpy==2.0"), &chunkSource{}, h.sink)
	require.NoError(t, err)

	execs := h.containers.Execs()
	require.Len(t, execs, 1)
	assert.Equal(t, "exec_container-test", execs[0].id)
	// The packages are positional parameters after the script, never part of
	// its text, so no requirement string can be read as shell syntax.
	assert.Equal(t, []string{"sh", "-c", execs[0].args[2], "sh", "flask", "numpy==2.0"}, execs[0].args)
	// --system installs into the first python3 on PATH, which the sandbox's PATH makes the
	// image's own Python; a wrong interpreter must fail the install, not be worked around.
	assert.Contains(t, execs[0].args[2], `uv pip install --system --no-cache-dir -- "$@"`)
	assert.NotContains(t, execs[0].args[2], "--python")
	assert.Contains(t, execs[0].args[2], "python -m readie_executor.network",
		"the DNS bootstrap must run first: this happens before the executor is reached")
	assert.Equal(t, []string{"Installed 1 package\n"}, h.sink.LogLines(),
		"the install output goes to the client as logs")
	assert.Equal(t, "result", string(h.sink.PayloadBytes()), "the call still runs afterwards")
}

func TestRun_NoPackagesMeansNoExec(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, nil)

	_, err := h.runner.Run(context.Background(), request(), &chunkSource{}, h.sink)
	require.NoError(t, err)

	assert.Empty(t, h.containers.Execs())
}

// Pinned: a failed install used to be a failure envelope from the executor,
// with the container kept. It must still not cost a session its warm container.
func TestRun_AFailedInstallReportsItsOutputAndKeepsTheContainer(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.execResult = sandbox.ExecResult{
			ExitCode: 1,
			Output:   []byte("error: No solution found: no-such-package\n"),
		}
	})

	_, err := h.runner.Run(context.Background(), withPackages("no-such-package"), &chunkSource{}, h.sink)

	require.ErrorIs(t, err, execution.ErrPackageInstall)
	assert.Equal(t, []string{"error: No solution found: no-such-package\n"}, h.sink.LogLines())
	assert.Empty(t, h.dialer.RequestBody(), "the executor never saw the request")
	require.NoError(t, h.runner.WaitPendingReleases(context.Background()))
	assert.Equal(t, []container.Outcome{container.OutcomeSuccess}, h.containers.Outcomes())
}

func TestRun_AnExecThatCannotRunIsAnInstallFailure(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.execErr = errors.New("runtime unavailable")
	})

	_, err := h.runner.Run(context.Background(), withPackages("flask"), &chunkSource{}, h.sink)

	require.ErrorIs(t, err, execution.ErrPackageInstall)
}

// Pinned: the install ran straight after Acquire, which returns before the sandbox exists, so an
// exec in a fresh container found nothing to run in. The executor's socket accepting a connection
// is the proof that the sandbox is up, so the install comes after the dial and still before any of
// the request is sent.
func TestRun_InstallsAfterTheExecutorSocketConnectsAndBeforeTheRequestIsSent(t *testing.T) {
	var dialsAtExec int
	var bodyAtExec []byte
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.onExec = func() {
			dialsAtExec = h.dialer.Dials()
			bodyAtExec = h.dialer.RequestBody()
		}
	})

	_, err := h.runner.Run(context.Background(), withPackages("flask"), &chunkSource{chunks: [][]byte{[]byte("payload")}}, h.sink)

	require.NoError(t, err)
	assert.Equal(t, 1, dialsAtExec, "the executor was already dialled when the install ran")
	assert.Empty(t, bodyAtExec, "none of the request had been sent yet")
	assert.Equal(t, "first-payload", string(h.dialer.RequestBody()), "and it is sent once the install is done")
	require.Len(t, h.containers.Execs(), 1)
}

func TestRun_NoInstallRunsWhenTheExecutorCannotBeDialled(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.dialer.dialErr = errors.New("socket never came up")
	})

	_, err := h.runner.Run(context.Background(), withPackages("flask"), &chunkSource{}, h.sink)

	require.Error(t, err)
	assert.NotErrorIs(t, err, execution.ErrPackageInstall)
	assert.Empty(t, h.containers.Execs(), "there is no running sandbox to install in")
}

// The connection is open while the install runs, so a failed install has to hang up on the
// executor, which then waits for the next call.
func TestRun_AFailedInstallHangsUpOnTheExecutor(t *testing.T) {
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.execResult = sandbox.ExecResult{ExitCode: 1, Output: []byte("error\n")}
	})

	_, err := h.runner.Run(context.Background(), withPackages("no-such-package"), &chunkSource{}, h.sink)

	require.ErrorIs(t, err, execution.ErrPackageInstall)
	require.Eventually(t, func() bool { return h.dialer.HungUp() == 1 }, 2*time.Second, 5*time.Millisecond,
		"the connection to the executor was closed without a request")
}

func TestRun_AnIdleConnectionWaitsOutALongInstall(t *testing.T) {
	// The install can take minutes; the connection to the executor sits idle until it is done.
	h := newHarness(t, execution.RunnerConfig{}, func(h *harness) {
		h.containers.onExec = func() { time.Sleep(300 * time.Millisecond) }
	})

	_, err := h.runner.Run(context.Background(), withPackages("flask"), &chunkSource{chunks: [][]byte{[]byte("payload")}}, h.sink)

	require.NoError(t, err)
	assert.Equal(t, "first-payload", string(h.dialer.RequestBody()))
	assert.Equal(t, 0, h.dialer.HungUp())
}
