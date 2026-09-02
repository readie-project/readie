package executor_test

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/illinoisdata/readie/worker/internal/clock"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/logging"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakeexecutor"
)

// pathResolver maps every container to one fixed socket path.
type pathResolver string

func (p pathResolver) SocketPath(string) string { return string(p) }

func dialFakeExecutor(t *testing.T, opts fakeexecutor.Options) (executor.Conn, *fakeexecutor.Server) {
	t.Helper()

	dir := fakeexecutor.ShortTempDir(t)
	path := filepath.Join(dir, "executor.sock")
	server := fakeexecutor.Start(t, path, opts)

	dialer := executor.NewUnixDialer(
		pathResolver(path),
		executor.RetryPolicy{Total: 5 * time.Second, Interval: 10 * time.Millisecond},
		clock.NewSystem(),
		logging.Discard(),
	)

	conn, err := dialer.Dial(context.Background(), "cid")
	require.NoError(t, err)
	t.Cleanup(func() { _ = conn.Close() })

	return conn, server
}

func newSession(t *testing.T, conn executor.Conn, opts executor.SessionOptions) *executor.Session {
	t.Helper()
	s := executor.NewSession(conn, opts, logging.Discard())
	t.Cleanup(func() { _ = s.Close() })
	return s
}

// frame renders one length-prefixed chunk.
func frame(p []byte) []byte {
	return append(executor.EncodeLength(len(p)), p...)
}

// readFramedMessage reads length-prefixed chunks until the terminator.
func readFramedMessage(r io.Reader) ([]byte, error) {
	var (
		body   []byte
		header = make([]byte, executor.LengthBytes)
	)
	for {
		if _, err := io.ReadFull(r, header); err != nil {
			return body, err
		}
		n, err := executor.DecodeLength(header)
		if err != nil {
			return body, err
		}
		if n == 0 {
			return body, nil
		}
		chunk := make([]byte, n)
		if _, err := io.ReadFull(r, chunk); err != nil {
			return body, err
		}
		body = append(body, chunk...)
	}
}

// Each chunk carries its own length prefix and the message ends with a
// zero-length one, so the executor never has to guess where a body stops.
func TestSession_FramesEachChunkAndTerminatesTheMessage(t *testing.T) {
	conn, server := dialFakeExecutor(t, fakeexecutor.Options{Reply: []byte("ok")})
	session := newSession(t, conn, executor.SessionOptions{})

	require.NoError(t, session.WriteChunk([]byte("pickled-")))
	require.NoError(t, session.WriteChunk([]byte("payload")))
	require.NoError(t, session.CloseRequest())

	var got bytes.Buffer
	require.NoError(t, session.ReadResponse(context.Background(), func(p []byte) error {
		got.Write(p)
		return nil
	}))

	assert.Equal(t, "ok", got.String())
	assert.Equal(t, "pickled-payload", string(server.LastRequest()),
		"the executor must see the payload with the terminator stripped")
}

func TestSession_StreamsALargePayloadInChunks(t *testing.T) {
	payload := bytes.Repeat([]byte("x"), 2*1024*1024+512)
	reply := bytes.Repeat([]byte("y"), 2*1024*1024+128)

	conn, server := dialFakeExecutor(t, fakeexecutor.Options{Reply: reply, ChunkSize: 1024 * 1024})
	session := newSession(t, conn, executor.SessionOptions{ChunkSize: 1024 * 1024})

	for offset := 0; offset < len(payload); offset += 1024 * 1024 {
		end := min(offset+1024*1024, len(payload))
		require.NoError(t, session.WriteChunk(payload[offset:end]))
	}
	require.NoError(t, session.CloseRequest())

	var (
		got    bytes.Buffer
		chunks int
	)
	require.NoError(t, session.ReadResponse(context.Background(), func(p []byte) error {
		chunks++
		got.Write(p)
		return nil
	}))

	assert.Equal(t, reply, got.Bytes())
	assert.Greater(t, chunks, 1, "a multi-megabyte reply must arrive as several chunks")
	assert.Equal(t, payload, server.LastRequest())
}

func TestSession_EmptyChunksAreNotWritten(t *testing.T) {
	conn, server := dialFakeExecutor(t, fakeexecutor.Options{Reply: []byte("ok")})
	session := newSession(t, conn, executor.SessionOptions{})

	require.NoError(t, session.WriteChunk(nil))
	require.NoError(t, session.WriteChunk([]byte{}))
	require.NoError(t, session.WriteChunk([]byte("body")))
	require.NoError(t, session.CloseRequest())
	require.NoError(t, session.ReadResponse(context.Background(), func([]byte) error { return nil }))

	assert.Equal(t, "body", string(server.LastRequest()))
}

// An executor that replies but never closes must not wedge the request; the
// idle timeout is the safety net for that.
func TestReadResponse_IdleTimeoutEndsAHeldOpenResponse(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{
		Mode:  fakeexecutor.ModeHoldOpen,
		Reply: []byte("partial result"),
	})
	session := newSession(t, conn, executor.SessionOptions{IdleTimeout: 200 * time.Millisecond})

	require.NoError(t, session.CloseRequest())

	var got bytes.Buffer
	start := time.Now()
	err := session.ReadResponse(context.Background(), func(p []byte) error {
		got.Write(p)
		return nil
	})
	elapsed := time.Since(start)

	require.NoError(t, err, "an idle connection after a complete reply is not an error")
	assert.Equal(t, "partial result", got.String())
	assert.Less(t, elapsed, 3*time.Second, "the idle timeout should have fired promptly")
}

func TestReadResponse_ReportsNoResponseWhenNothingArrives(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Mode: fakeexecutor.ModeNoResponse})
	session := newSession(t, conn, executor.SessionOptions{
		FirstByteTimeout: 200 * time.Millisecond,
		IdleTimeout:      200 * time.Millisecond,
	})

	require.NoError(t, session.CloseRequest())

	err := session.ReadResponse(context.Background(), func([]byte) error { return nil })
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrNoResponse)
}

// Under version 1 an immediate close *was* the response: an empty one, which
// is what a raising function produced. There is no longer any such thing -
// every outcome, including a failure, is a message - so a peer that closes
// without sending one has died.
func TestReadResponse_AClosedConnectionWithNoMessageIsAnError(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Mode: fakeexecutor.ModeCloseImmediately})
	session := newSession(t, conn, executor.SessionOptions{FirstByteTimeout: 2 * time.Second})

	require.NoError(t, session.CloseRequest())

	err := session.ReadResponse(context.Background(), func([]byte) error { return nil })
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrNoResponse)
}

// The defect the framing exists to fix. The executor writes a chunk header
// promising more than it sends and then dies; version 1 saw a short body and
// reported success, handing the client a payload it could not unpickle.
func TestReadResponse_ATruncatedResponseIsAnErrorNotAShortSuccess(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{
		Mode:  fakeexecutor.ModeTruncatedResponse,
		Reply: []byte("partial"),
	})
	session := newSession(t, conn, executor.SessionOptions{
		FirstByteTimeout: 2 * time.Second,
		IdleTimeout:      200 * time.Millisecond,
	})

	require.NoError(t, session.CloseRequest())

	err := session.ReadResponse(context.Background(), func([]byte) error { return nil })
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrTruncatedResponse)
}

// A body may now contain any bytes at all, including the ones that used to be
// the terminator.
func TestReadResponse_ABodyMayContainTheOldTerminatorBytes(t *testing.T) {
	for _, reply := range [][]byte{[]byte("EOF"), []byte("payload-EOF"), []byte("EOFEOF")} {
		t.Run(string(reply), func(t *testing.T) {
			conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Reply: reply})
			session := newSession(t, conn, executor.SessionOptions{})

			require.NoError(t, session.CloseRequest())

			var got bytes.Buffer
			require.NoError(t, session.ReadResponse(context.Background(), func(p []byte) error {
				got.Write(p)
				return nil
			}))
			assert.Equal(t, reply, got.Bytes())
		})
	}
}

// A chunk header this side will not honour must be refused before it is used
// as an allocation size: one corrupt prefix would otherwise try to allocate
// exabytes and take the worker down.
func TestReadResponse_AnImplausibleChunkLengthIsRefused(t *testing.T) {
	client, server := net.Pipe()
	t.Cleanup(func() { _ = client.Close(); _ = server.Close() })

	go func() {
		_, _ = readFramedMessage(server)
		_, _ = server.Write(executor.EncodeLength(1 << 62))
	}()

	session := newSession(t, client, executor.SessionOptions{FirstByteTimeout: 2 * time.Second})
	require.NoError(t, session.CloseRequest())

	err := session.ReadResponse(context.Background(), func([]byte) error { return nil })
	require.Error(t, err)
	assert.ErrorIs(t, err, executor.ErrMalformedFrame)
}

// Cancellation must interrupt a read that is already blocked. The previous
// implementation polled the context with a non-blocking select before each
// read, which could never fire while a read was in flight.
func TestReadResponse_CancellationInterruptsABlockedRead(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Mode: fakeexecutor.ModeNoResponse})
	session := newSession(t, conn, executor.SessionOptions{
		FirstByteTimeout: time.Hour, // would hang forever without cancellation
		IdleTimeout:      time.Hour,
	})
	require.NoError(t, session.CloseRequest())

	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(100*time.Millisecond, cancel)

	start := time.Now()
	err := session.ReadResponse(ctx, func([]byte) error { return nil })
	elapsed := time.Since(start)

	require.Error(t, err)
	assert.ErrorIs(t, err, context.Canceled)
	assert.Less(t, elapsed, 5*time.Second, "cancellation must not wait for the read deadline")
}

func TestReadResponse_AlreadyCancelledContextReturnsImmediately(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Mode: fakeexecutor.ModeNoResponse})
	session := newSession(t, conn, executor.SessionOptions{FirstByteTimeout: time.Hour})

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	err := session.ReadResponse(ctx, func([]byte) error { return nil })
	assert.ErrorIs(t, err, context.Canceled)
}

func TestReadResponse_PropagatesSinkErrors(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Reply: []byte("data")})
	session := newSession(t, conn, executor.SessionOptions{})
	require.NoError(t, session.CloseRequest())

	sentinel := errors.New("client went away")
	err := session.ReadResponse(context.Background(), func([]byte) error { return sentinel })
	assert.ErrorIs(t, err, sentinel)
}

func TestSession_CloseIsIdempotent(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Reply: []byte("ok")})
	session := executor.NewSession(conn, executor.SessionOptions{}, logging.Discard())

	require.NoError(t, session.Close())
	require.NoError(t, session.Close())
}

func TestWriteChunk_FailsAfterTheConnectionIsClosed(t *testing.T) {
	conn, _ := dialFakeExecutor(t, fakeexecutor.Options{Reply: []byte("ok")})
	session := executor.NewSession(conn, executor.SessionOptions{}, logging.Discard())
	require.NoError(t, session.Close())

	assert.Error(t, session.WriteChunk([]byte("late")))
}

// net.Pipe satisfies executor.Conn, which is what lets the orchestration layer
// be tested without touching the filesystem.
func TestSession_WorksOverNetPipe(t *testing.T) {
	client, server := net.Pipe()
	t.Cleanup(func() { _ = client.Close(); _ = server.Close() })

	go func() {
		if _, err := readFramedMessage(server); err != nil {
			return
		}
		_, _ = server.Write(frame([]byte("piped")))
		_, _ = server.Write(executor.Terminator())
		_ = server.Close()
	}()

	session := newSession(t, client, executor.SessionOptions{})
	require.NoError(t, session.WriteChunk([]byte("req")))
	require.NoError(t, session.CloseRequest())

	var got bytes.Buffer
	require.NoError(t, session.ReadResponse(context.Background(), func(p []byte) error {
		got.Write(p)
		return nil
	}))
	assert.Equal(t, "piped", got.String())
}
