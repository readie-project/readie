package execution

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestClassify_ACaughtUpCallerContextIsNotAFailure(t *testing.T) {
	r := &Runner{}
	execCtx, cancel := context.WithCancel(context.Background())
	cancel() // the response already went out; the caller has since hung up

	err := r.classify(execCtx, execCtx, nil, nil)

	require.NoError(t, err,
		"a caller ending its context right after receiving a full response is not a failure to blame the execution for")
}

func TestClassify_PrefersClientClosedWhenTheCallerCancelledAFailedSend(t *testing.T) {
	r := &Runner{}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := r.classify(ctx, context.Background(), errors.New("broken pipe"), nil)

	require.Error(t, err)
	assert.ErrorIs(t, err, ErrClientClosed)
}

func TestClassify_ReportsATimeoutOverAGenericWaitError(t *testing.T) {
	r := &Runner{}
	execCtx, cancel := context.WithTimeout(context.Background(), 0)
	defer cancel()
	<-execCtx.Done()

	err := r.classify(context.Background(), execCtx, nil, errors.New("stream reset"))

	require.Error(t, err)
	assert.ErrorIs(t, err, ErrExecutionTimeout)
}

func TestClassify_FallsBackToTheRawSendOrWaitError(t *testing.T) {
	r := &Runner{}

	sendErr := errors.New("relay broke")
	err := r.classify(context.Background(), context.Background(), sendErr, nil)
	require.Error(t, err)
	assert.ErrorIs(t, err, sendErr)

	waitErr := errors.New("pump broke")
	err = r.classify(context.Background(), context.Background(), nil, waitErr)
	require.Error(t, err)
	assert.ErrorIs(t, err, waitErr)
}
