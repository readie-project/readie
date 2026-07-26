package executor_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/clock"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/executor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeexecutor"
)

func newDialer(path string, retry executor.RetryPolicy) *executor.UnixDialer {
	return executor.NewUnixDialer(pathResolver(path), retry, clock.NewSystem(), logging.Discard())
}

// The real executor sleeps 30 seconds awaiting a checkpoint before it binds its
// socket, so the dialer has to keep retrying across a startup gap rather than
// failing on the first refused connection.
func TestDial_RetriesUntilTheExecutorBinds(t *testing.T) {
	if testing.Short() {
		t.Skip("timing-sensitive")
	}

	dir := fakeexecutor.ShortTempDir(t)
	path := filepath.Join(dir, "executor.sock")
	fakeexecutor.Start(t, path, fakeexecutor.Options{BindDelay: 500 * time.Millisecond})

	dialer := newDialer(path, executor.RetryPolicy{Total: 5 * time.Second, Interval: 20 * time.Millisecond})

	conn, err := dialer.Dial(context.Background(), "cid")
	require.NoError(t, err)
	require.NoError(t, conn.Close())
}

func TestDial_ReportsTimeoutWhenTheBudgetIsTooShort(t *testing.T) {
	if testing.Short() {
		t.Skip("timing-sensitive")
	}

	dir := fakeexecutor.ShortTempDir(t)
	path := filepath.Join(dir, "executor.sock")
	fakeexecutor.Start(t, path, fakeexecutor.Options{BindDelay: 10 * time.Second})

	dialer := newDialer(path, executor.RetryPolicy{Total: 300 * time.Millisecond, Interval: 20 * time.Millisecond})

	_, err := dialer.Dial(context.Background(), "cid")
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrDialTimeout)
}

func TestDial_NonexistentSocketTimesOutRatherThanFailingFast(t *testing.T) {
	dir := fakeexecutor.ShortTempDir(t)
	dialer := newDialer(filepath.Join(dir, "never.sock"), executor.RetryPolicy{
		Total:    200 * time.Millisecond,
		Interval: 20 * time.Millisecond,
	})

	_, err := dialer.Dial(context.Background(), "cid")
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrDialTimeout,
		"a socket that has not appeared yet is indistinguishable from one that is still starting")
}

func TestDial_HonoursContextCancellation(t *testing.T) {
	dir := fakeexecutor.ShortTempDir(t)
	dialer := newDialer(filepath.Join(dir, "never.sock"), executor.RetryPolicy{
		Total:    time.Minute,
		Interval: 20 * time.Millisecond,
	})

	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(100*time.Millisecond, cancel)

	start := time.Now()
	_, err := dialer.Dial(ctx, "cid")
	require.Error(t, err)
	assert.Less(t, time.Since(start), 5*time.Second, "cancellation must abandon the retry budget")
}

func TestDial_SucceedsImmediatelyWhenTheSocketIsReady(t *testing.T) {
	dir := fakeexecutor.ShortTempDir(t)
	path := filepath.Join(dir, "executor.sock")
	server := fakeexecutor.Start(t, path, fakeexecutor.Options{})
	require.True(t, server.WaitUntilListening(2*time.Second))

	dialer := newDialer(path, executor.RetryPolicy{Total: 2 * time.Second, Interval: 10 * time.Millisecond})

	conn, err := dialer.Dial(context.Background(), "cid")
	require.NoError(t, err)
	require.NoError(t, conn.Close())
}
