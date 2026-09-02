package grpcserver

import (
	"context"
	"errors"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/executor"
	"github.com/illinoisdata/readie/worker/internal/registry"
	"github.com/illinoisdata/readie/worker/internal/sandbox"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// The protos deliberately declare no package, so the full method name is
// /ExecutionService/RequestExecution. Adding a proto package would silently
// change it and break the router, which is why this is asserted rather than
// left to review.
func TestContract_ServiceAndMethodNames(t *testing.T) {
	assert.Equal(t, "ExecutionService", pb.ExecutionService_ServiceDesc.ServiceName)

	require.Len(t, pb.ExecutionService_ServiceDesc.Streams, 1)
	stream := pb.ExecutionService_ServiceDesc.Streams[0]
	assert.Equal(t, "RequestExecution", stream.StreamName)
	assert.True(t, stream.ClientStreams, "the request half must stay streaming")
	assert.True(t, stream.ServerStreams, "the response half must stay streaming")

	assert.Equal(t, "RegistryService", pb.RegistryService_ServiceDesc.ServiceName)
	methods := make([]string, 0, len(pb.RegistryService_ServiceDesc.Methods))
	for _, m := range pb.RegistryService_ServiceDesc.Methods {
		methods = append(methods, m.MethodName)
	}
	assert.ElementsMatch(t, []string{
		"PostWorkerStatus", "PostExecutorStatus",
		"PostWorkerUtilization", "PostExecutorUtilization",
	}, methods)
}

// An absent optional container_id means "provision a new container". The
// previous implementation dereferenced the pointer and panicked.
func TestToExecutionRequest_AbsentContainerIDMeansProvisionNew(t *testing.T) {
	req, err := toExecutionRequest(&pb.WorkerExecutionRequest{
		RequestId: "req-1",
		SessionId: "sess-1",
		Budgets:   []*pb.ResourceBudget{{Kind: pb.ResourceKind_RESOURCE_KIND_MEMORY, Alloc: 512 << 20}},
	})
	require.NoError(t, err)

	assert.Empty(t, req.ContainerID)
	assert.Equal(t, "req-1", req.Ref.RequestID)
	assert.Equal(t, "sess-1", req.Ref.SessionID)
	assert.Equal(t, int64(512<<20), req.Alloc.Memory().Alloc)
}

func TestToExecutionRequest_CarriesEveryField(t *testing.T) {
	containerID := "exec_container-abc"
	req, err := toExecutionRequest(&pb.WorkerExecutionRequest{
		RequestId:    "req-1",
		SessionId:    "sess-1",
		ContainerId:  &containerID,
		CheckpointId: "checkpoint_1",
		Payload:      []byte("body"),
		Budgets: []*pb.ResourceBudget{
			{Kind: pb.ResourceKind_RESOURCE_KIND_MEMORY, Alloc: 1 << 20},
			{Kind: pb.ResourceKind_RESOURCE_KIND_GPU_MEMORY, Alloc: 2},
		},
	})
	require.NoError(t, err)

	assert.Equal(t, containerID, req.ContainerID)
	assert.Equal(t, "checkpoint_1", req.CheckpointID)
	assert.Equal(t, []byte("body"), req.InitialPayload)
	assert.Equal(t, int64(1<<20), req.Alloc.Memory().Alloc)
	assert.Equal(t, int64(2), req.Alloc.GPU().Alloc)
}

func TestToExecutionRequest_RejectsNil(t *testing.T) {
	_, err := toExecutionRequest(nil)
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrInvalidRequest)
}

// The router reads the provisioning fields off the first response, so every
// message must carry them - including request_id and session_id, which the
// router copies straight through to its own client.
func TestStreamSink_StampsProvisioningOntoEveryMessage(t *testing.T) {
	stream := &recordingStream{}
	sink := &streamSink{stream: stream}

	require.NoError(t, sink.Provision(execution.ProvisionInfo{
		Ref:          registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"},
		WorkerID:     "worker-1",
		ContainerID:  "exec_container-abc",
		CheckpointID: "checkpoint_1",
		Budgets: []container.Budget{
			{Kind: container.KindMemory, Alloc: 512 << 20},
			{Kind: container.KindGPUMemory, Alloc: 1},
		},
	}))
	assert.Empty(t, stream.sent, "Provision must not emit a frame of its own")

	require.NoError(t, sink.Payload([]byte("chunk")))
	require.NoError(t, sink.Logs("a log line\n"))

	require.Len(t, stream.sent, 2)
	for _, msg := range stream.sent {
		assert.Equal(t, "req-1", msg.GetRequestId())
		assert.Equal(t, "sess-1", msg.GetSessionId())
		assert.Equal(t, "worker-1", msg.GetWorkerId())
		assert.Equal(t, "exec_container-abc", msg.GetContainerId())
		assert.Equal(t, "checkpoint_1", msg.GetCheckpointId())
		assert.Equal(t, int64(512<<20), budgetAlloc(msg.GetBudgets(), pb.ResourceKind_RESOURCE_KIND_MEMORY))
		assert.Equal(t, int64(1), budgetAlloc(msg.GetBudgets(), pb.ResourceKind_RESOURCE_KIND_GPU_MEMORY))
		assert.True(t, msg.GetSuccess())
	}

	assert.Equal(t, []byte("chunk"), stream.sent[0].GetPayload())
	assert.Equal(t, "a log line\n", stream.sent[1].GetLogs())
}

func TestToStatus(t *testing.T) {
	tests := []struct {
		name string
		err  error
		want codes.Code
	}{
		{"nil", nil, codes.OK},
		{"invalid request", ErrInvalidRequest, codes.InvalidArgument},
		{"client closed", execution.ErrClientClosed, codes.Canceled},
		{"context cancelled", context.Canceled, codes.Canceled},
		{"execution timeout", execution.ErrExecutionTimeout, codes.DeadlineExceeded},
		{"no executor response", executor.ErrNoResponse, codes.DeadlineExceeded},
		{"deadline exceeded", context.DeadlineExceeded, codes.DeadlineExceeded},
		{"container missing", sandbox.ErrNotFound, codes.NotFound},
		{"daemon down", sandbox.ErrRuntimeUnavailable, codes.Unavailable},
		{"executor unreachable", executor.ErrDialTimeout, codes.Unavailable},
		{"router down", registry.ErrRouterUnavailable, codes.Unavailable},
		{"cannot provision", container.ErrAcquireFailed, codes.ResourceExhausted},
		{"unknown", errors.New("something else"), codes.Internal},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := toStatus(tt.err)
			if tt.want == codes.OK {
				assert.NoError(t, got)
				return
			}
			require.Error(t, got)
			assert.Equal(t, tt.want, status.Code(got))
		})
	}
}

// Errors reach toStatus wrapped several layers deep, so matching must survive
// wrapping.
func TestToStatus_MatchesThroughWrapping(t *testing.T) {
	wrapped := errors.Join(
		errors.New("relay response"),
		errors.Join(errors.New("acquire container"), container.ErrAcquireFailed),
	)
	assert.Equal(t, codes.ResourceExhausted, status.Code(toStatus(wrapped)))
}

func TestToStatus_DoesNotLeakInternalDetail(t *testing.T) {
	err := toStatus(errors.New("dial unix /shared/exec_container-abc/executor.sock: connection refused"))
	require.Error(t, err)

	assert.Equal(t, codes.Internal, status.Code(err))
	assert.NotContains(t, status.Convert(err).Message(), "/shared/",
		"filesystem layout must not reach the caller")
}

func TestToStatus_PreservesAnExistingStatusCode(t *testing.T) {
	err := toStatus(status.Error(codes.PermissionDenied, "nope"))
	assert.Equal(t, codes.PermissionDenied, status.Code(err))
}

// budgetAlloc returns the alloc of a budget of the given kind, or 0.
func budgetAlloc(budgets []*pb.ResourceBudget, kind pb.ResourceKind) int64 {
	for _, b := range budgets {
		if b.GetKind() == kind {
			return b.GetAlloc()
		}
	}
	return 0
}

// recordingStream captures what the sink sends.
type recordingStream struct {
	executionStream
	sent []*pb.WorkerExecutionResponse
}

func (r *recordingStream) Send(msg *pb.WorkerExecutionResponse) error {
	r.sent = append(r.sent, msg)
	return nil
}
