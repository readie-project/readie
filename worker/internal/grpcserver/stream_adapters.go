package grpcserver

import (
	"context"
	"fmt"

	"google.golang.org/grpc"

	"github.com/illinoisdata/readie/worker/internal/container"
	"github.com/illinoisdata/readie/worker/internal/execution"
	"github.com/illinoisdata/readie/worker/internal/registry"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// executionStream is the bidirectional stream this service serves.
type executionStream = grpc.BidiStreamingServer[pb.WorkerExecutionRequest, pb.WorkerExecutionResponse]

// toExecutionRequest validates the first message and converts it.
func toExecutionRequest(msg *pb.WorkerExecutionRequest) (execution.Request, error) {
	if msg == nil {
		return execution.Request{}, fmt.Errorf("%w: empty first message", ErrInvalidRequest)
	}

	// GetContainerId returns "" for an absent optional field, which is already
	// the "provision a new container" sentinel. The previous implementation
	// dereferenced the pointer directly and panicked when the router omitted it.
	return execution.Request{
		Ref: registry.ExecutionRef{
			RequestID: msg.GetRequestId(),
			SessionID: msg.GetSessionId(),
		},
		ContainerID:  msg.GetContainerId(),
		CheckpointID: msg.GetCheckpointId(),
		Alloc:        container.Allocation{Budgets: budgetsFromProto(msg.GetBudgets())},

		InitialPayload: msg.GetPayload(),
	}, nil
}

// budgetsFromProto converts the wire budgets to the container package's own.
func budgetsFromProto(wire []*pb.ResourceBudget) []container.Budget {
	if len(wire) == 0 {
		return nil
	}
	budgets := make([]container.Budget, 0, len(wire))
	for _, b := range wire {
		budgets = append(budgets, container.Budget{
			Kind:  int32(b.GetKind()),
			Alloc: b.GetAlloc(),
			Max:   b.GetMax(),
		})
	}
	return budgets
}

// budgetsToProto converts the container package's budgets back to the wire.
func budgetsToProto(budgets []container.Budget) []*pb.ResourceBudget {
	if len(budgets) == 0 {
		return nil
	}
	wire := make([]*pb.ResourceBudget, 0, len(budgets))
	for _, b := range budgets {
		wire = append(wire, &pb.ResourceBudget{
			Kind:  pb.ResourceKind(b.Kind),
			Alloc: b.Alloc,
			Max:   b.Max,
		})
	}
	return wire
}

// streamSource adapts the inbound half of the stream to a PayloadSource.
type streamSource struct {
	stream executionStream
}

var _ execution.PayloadSource = streamSource{}

// Next returns the next request chunk, or io.EOF once the caller has finished.
func (s streamSource) Next(context.Context) ([]byte, error) {
	msg, err := s.stream.Recv()
	if err != nil {
		// io.EOF is the caller closing its half, which the runner treats as
		// the normal end of the request body.
		return nil, err //nolint:wrapcheck // io.EOF must stay comparable
	}
	return msg.GetPayload(), nil
}

// streamSink is the only thing in the process that sends on the stream.
//
// It holds the provisioning fields the router expects on the first response
// and stamps them onto every message.
type streamSink struct {
	stream executionStream
	info   execution.ProvisionInfo
}

var _ execution.Sink = (*streamSink)(nil)

// Provision records the scheduling decision without sending anything.
//
// The router reads these fields off the first response it receives, so
// carrying them on the next real message satisfies the contract without
// inventing an extra frame. Keeping that choice here, in the transport
// adapter, means the runner never has to know about it.
func (s *streamSink) Provision(info execution.ProvisionInfo) error {
	s.info = info
	return nil
}

// Payload sends a chunk of the executor's response.
func (s *streamSink) Payload(p []byte) error {
	msg := s.newMessage()
	msg.Data = &pb.WorkerExecutionResponse_Payload{Payload: p}
	return s.send(msg)
}

// Logs sends a line of container output.
func (s *streamSink) Logs(line string) error {
	msg := s.newMessage()
	msg.Data = &pb.WorkerExecutionResponse_Logs{Logs: line}
	return s.send(msg)
}

func (s *streamSink) send(msg *pb.WorkerExecutionResponse) error {
	if err := s.stream.Send(msg); err != nil {
		return fmt.Errorf("send response: %w", err)
	}
	return nil
}

// newMessage builds a response carrying the provisioning fields.
//
// A fresh literal is constructed each time rather than copying a template:
// protobuf messages embed a mutex-bearing state field that must not be copied.
func (s *streamSink) newMessage() *pb.WorkerExecutionResponse {
	return &pb.WorkerExecutionResponse{
		// The router copies these through to its own client. The previous
		// implementation never set them, so every client response arrived
		// with empty identifiers.
		RequestId:    s.info.Ref.RequestID,
		SessionId:    s.info.Ref.SessionID,
		WorkerId:     s.info.WorkerID,
		ContainerId:  s.info.ContainerID,
		CheckpointId: s.info.CheckpointID,
		Budgets:      budgetsToProto(s.info.Budgets),
		Success:      true,
	}
}
