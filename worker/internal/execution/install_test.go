package execution_test

import (
	"context"
	"errors"
	"testing"

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
	assert.Contains(t, execs[0].args[2], `uv pip install --system --no-cache-dir -- "$@"`)
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
