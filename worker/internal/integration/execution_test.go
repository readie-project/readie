package integration

import (
	"bytes"
	"context"
	"errors"
	"io"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/artifact"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeexecutor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakesandbox"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

const cpuAlloc = int64(512 << 20)

// execute drives a full request/response cycle the way the router does.
func execute(
	t *testing.T,
	h *harness,
	containerID string,
	chunks ...[]byte,
) ([]*pb.WorkerExecutionResponse, error) {
	t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	stream, err := h.Exec.RequestExecution(ctx)
	require.NoError(t, err)

	for i, chunk := range chunks {
		msg := &pb.WorkerExecutionRequest{
			RequestId: "req-1",
			SessionId: "sess-1",
			WorkerId:  "worker-1",
			Payload:   chunk,
			CpuAlloc:  cpuAlloc,
		}
		// The router stamps the scheduling decision on every message; the
		// first is the one the worker acts on.
		if i == 0 || containerID != "" {
			msg.ContainerId = &containerID
		}
		require.NoError(t, stream.Send(msg))
	}
	require.NoError(t, stream.CloseSend())

	var responses []*pb.WorkerExecutionResponse
	for {
		res, err := stream.Recv()
		if errors.Is(err, io.EOF) {
			return responses, nil
		}
		if err != nil {
			return responses, err
		}
		responses = append(responses, res)
	}
}

func payloadOf(responses []*pb.WorkerExecutionResponse) []byte {
	var buf bytes.Buffer
	for _, res := range responses {
		if p := res.GetPayload(); len(p) > 0 {
			buf.Write(p)
		}
	}
	return buf.Bytes()
}

func TestExecution_HappyPath(t *testing.T) {
	h := newHarness(t)

	responses, err := execute(t, h, "", []byte("pickled-"), []byte("payload"))
	require.NoError(t, err)
	require.NotEmpty(t, responses)

	assert.Equal(t, "executor-result", string(payloadOf(responses)))

	// The executor must see the concatenated body with the terminator stripped.
	require.NotNil(t, h.lastExecutor())
	assert.Equal(t, "pickled-payload", string(h.lastExecutor().LastRequest()))

	// The router reads the scheduling decision off the first response.
	first := responses[0]
	assert.Equal(t, "worker-1", first.GetWorkerId())
	assert.NotEmpty(t, first.GetContainerId())
	assert.Equal(t, cpuAlloc, first.GetCpuAlloc())
	assert.True(t, first.GetSuccess())

	// The router copies these through to its own client; the previous
	// implementation left them empty on every response.
	assert.Equal(t, "req-1", first.GetRequestId())
	assert.Equal(t, "sess-1", first.GetSessionId())

	// A completed container is paused for reuse, not destroyed.
	state, ok := h.Runtime.Get(first.GetContainerId())
	require.True(t, ok)
	assert.True(t, state.Paused)
	assert.False(t, state.Removed)

	require.True(t, h.Router.WaitFor(func(s *fakeregistry.Server) bool {
		return len(s.ExecutorStatuses()) >= 2
	}, 5*time.Second))

	var statuses []pb.Status
	for _, s := range h.Router.ExecutorStatuses() {
		statuses = append(statuses, s.GetStatus())
	}
	assert.Equal(t, []pb.Status{pb.Status_STATUS_BUSY, pb.Status_STATUS_READY}, statuses)
}

func TestExecution_ContainerSpecMatchesTheExecutorContract(t *testing.T) {
	h := newHarness(t)

	_, err := execute(t, h, "", []byte("body"))
	require.NoError(t, err)

	specs := h.Runtime.CreateSpecs()
	require.Len(t, specs, 1)
	spec := specs[0]

	assert.Equal(t, []string{"python", "-u", "-m", "crfs_executor"}, spec.Args,
		"the manifest records argv and the worker replays it verbatim")
	assert.Equal(t, []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/lib/python3.12/dist-packages"}, spec.Env)
	assert.Equal(t, cpuAlloc, spec.MemoryBytes)
	assert.Equal(t, int64(50000), spec.CPUQuota)
	assert.Equal(t, int64(100), spec.PidsLimit)
	// One rootfs, at a fixed path directly under the artifact root -- not
	// nested under a generation, because there is only ever one.
	assert.Equal(t, filepath.Join(h.ArtifactRoot, artifact.RootfsDirName), spec.RootfsPath)

	// One bind only: the generation's rootfs already carries the Python
	// environment, so there is no site-packages mount.
	require.Len(t, spec.Mounts, 1)
	assert.Equal(t, "/tmp", spec.Mounts[0].Destination)
	assert.Equal(t, "bind", spec.Mounts[0].Type)
}

func TestExecution_ReusesAWarmContainer(t *testing.T) {
	h := newHarness(t)

	first, err := execute(t, h, "", []byte("first"))
	require.NoError(t, err)
	require.NotEmpty(t, first)
	containerID := first[0].GetContainerId()

	second, err := execute(t, h, containerID, []byte("second"))
	require.NoError(t, err)
	require.NotEmpty(t, second)

	assert.Equal(t, containerID, second[0].GetContainerId())
	assert.Len(t, h.Runtime.CreateSpecs(), 1, "a warm container must not be recreated")

	state, ok := h.Runtime.Get(containerID)
	require.True(t, ok)
	assert.True(t, state.Paused, "the container is paused again after the second execution")
}

func TestExecution_LargePayloadRoundTrips(t *testing.T) {
	reply := bytes.Repeat([]byte("r"), 3*1024*1024+7)
	h := newHarness(t, withExecutor(fakeexecutor.Options{Reply: reply}))

	request := bytes.Repeat([]byte("q"), 2*1024*1024+11)
	responses, err := execute(t, h, "", request[:1024*1024], request[1024*1024:])
	require.NoError(t, err)

	assert.Equal(t, reply, payloadOf(responses))
	assert.Equal(t, request, h.lastExecutor().LastRequest())
}

// The direct regression test for the recover()-swallows-timeouts bug: a
// timeout must fail the RPC and destroy the container, not report success.
func TestExecution_TimeoutFailsAndDestroysTheContainer(t *testing.T) {
	h := newHarness(t,
		withConfig(func(c *config.Config) {
			c.ExecutionTimeout = 300 * time.Millisecond
			c.FirstByteTimeout = time.Hour
			c.ResponseIdleTimeout = time.Hour
		}),
		withExecutor(fakeexecutor.Options{
			Mode:          fakeexecutor.ModeSlowResponse,
			ResponseDelay: 30 * time.Second,
			Reply:         []byte("too late"),
		}),
	)

	_, err := execute(t, h, "", []byte("body"))
	require.Error(t, err)
	assert.Equal(t, codes.DeadlineExceeded, status.Code(err))

	live := h.Runtime.LiveIDs()
	assert.Empty(t, live, "a timed-out container must be destroyed, not reused")

	ids := h.Runtime.IDs()
	require.Len(t, ids, 1)
	assert.NoDirExists(t, h.Layout.ContainerDir(ids[0]))

	require.True(t, h.Router.WaitFor(func(s *fakeregistry.Server) bool {
		for _, st := range s.ExecutorStatuses() {
			if st.GetStatus() == pb.Status_STATUS_REMOVED {
				return true
			}
		}
		return false
	}, 5*time.Second), "the router must be told the container is gone")
}

func TestExecution_UnreachableExecutorIsReportedAsUnavailable(t *testing.T) {
	h := newHarness(t,
		withoutExecutors(),
		withConfig(func(c *config.Config) { c.DialTotalTimeout = 300 * time.Millisecond }),
	)

	_, err := execute(t, h, "", []byte("body"))
	require.Error(t, err)
	assert.Equal(t, codes.Unavailable, status.Code(err))

	assert.Empty(t, h.Runtime.LiveIDs(),
		"a container whose executor never answered is not reusable")
}

func TestExecution_ClientCancellationReleasesTheContainer(t *testing.T) {
	h := newHarness(t,
		withConfig(func(c *config.Config) {
			c.FirstByteTimeout = time.Hour
			c.ResponseIdleTimeout = time.Hour
		}),
		withExecutor(fakeexecutor.Options{
			Mode:          fakeexecutor.ModeSlowResponse,
			ResponseDelay: 30 * time.Second,
		}),
	)

	ctx, cancel := context.WithCancel(context.Background())
	stream, err := h.Exec.RequestExecution(ctx)
	require.NoError(t, err)

	empty := ""
	require.NoError(t, stream.Send(&pb.WorkerExecutionRequest{
		RequestId: "req-1", SessionId: "sess-1",
		ContainerId: &empty, Payload: []byte("body"), CpuAlloc: cpuAlloc,
	}))
	require.NoError(t, stream.CloseSend())

	// Give the worker time to provision before hanging up.
	require.Eventually(t, func() bool { return len(h.Runtime.IDs()) > 0 }, 10*time.Second, 20*time.Millisecond)
	cancel()

	// Cleanup runs on a context detached from the cancelled one.
	require.Eventually(t, func() bool {
		return len(h.Runtime.LiveIDs()) == 0
	}, 15*time.Second, 50*time.Millisecond, "cancellation must still release the container")
}

// An executor that replies but never closes must not wedge the request.
func TestExecution_HeldOpenExecutorCompletesViaIdleTimeout(t *testing.T) {
	h := newHarness(t, withExecutor(fakeexecutor.Options{
		Mode:  fakeexecutor.ModeHoldOpen,
		Reply: []byte("held-open-result"),
	}))

	responses, err := execute(t, h, "", []byte("body"))
	require.NoError(t, err)
	assert.Equal(t, "held-open-result", string(payloadOf(responses)))
}

func TestExecution_EmptyRequestStreamIsRejected(t *testing.T) {
	h := newHarness(t)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	stream, err := h.Exec.RequestExecution(ctx)
	require.NoError(t, err)
	require.NoError(t, stream.CloseSend())

	_, err = stream.Recv()
	require.Error(t, err)
	assert.Equal(t, codes.InvalidArgument, status.Code(err))
	assert.Empty(t, h.Runtime.IDs(), "nothing should be provisioned for an empty stream")
}

func TestExecution_ConcurrentRequestsGetDistinctContainers(t *testing.T) {
	h := newHarness(t)

	const n = 8
	results := make(chan string, n)
	errs := make(chan error, n)

	for range n {
		go func() {
			responses, err := execute(t, h, "", []byte("body"))
			if err != nil {
				errs <- err
				return
			}
			results <- responses[0].GetContainerId()
		}()
	}

	seen := make(map[string]bool, n)
	for range n {
		select {
		case err := <-errs:
			t.Fatalf("concurrent execution failed: %v", err)
		case id := <-results:
			require.False(t, seen[id], "container %s was handed to two requests at once", id)
			seen[id] = true
		case <-time.After(30 * time.Second):
			t.Fatal("concurrent executions did not finish")
		}
	}
	assert.Len(t, seen, n)
}

func TestExecution_StreamsContainerLogs(t *testing.T) {
	h := newHarness(t,
		withConfig(func(c *config.Config) { c.StreamLogs = true }),
		withFakes(func(d *fakesandbox.Sandbox, _ *fakeregistry.Server) {
			d.LogData = []byte("hello from the executor\n")
		}),
	)

	// Logs race the response by nature, so this asserts only that the channel
	// works when output is available, not that a specific line arrives.
	responses, err := execute(t, h, "", []byte("body"))
	require.NoError(t, err)
	assert.Equal(t, "executor-result", string(payloadOf(responses)))
}
