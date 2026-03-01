/* IMP: Only gRPC functions and operations should be panicking */
package rpc

import (
	"context"
	"io"
	"log"
	"sync"
	"worker/config"
	"worker/core"
	pb "worker/proto"
	"worker/socket"

	"google.golang.org/grpc"
)

type server struct {
	pb.UnimplementedExecutionServiceServer
}

func (s *server) RequestProvision(request *pb.ExecutorProvisionRequest) pb.ExecutorProvisionResponse {
	ctx := context.Background()
	ctx = context.WithValue(ctx, core.ContextIdentifierKey, &core.ExecutionIdentifier{
		RequestId:   request.RequestId,
		SessionId:   request.SessionId,
		ContainerId: "",
	})

	containerId, checkpointId, err := core.CreateAndStartContainer(ctx, &core.ContainerConfig{
		CheckpointId: request.CheckpointId,
		ResourceAllocation: core.ResourceAllocation{
			CpuAlloc: request.CpuAlloc,
			GpuAlloc: request.GpuAlloc,
		},
	})

	if err != nil {
		log.Panicf("Failed to provision container: %v", err)
	}

	return pb.ExecutorProvisionResponse{
		WorkerId:     config.WorkerId,
		ContainerId:  containerId,
		CheckpointId: checkpointId,
		CpuAlloc:     request.CpuAlloc,
		GpuAlloc:     request.GpuAlloc,
	}
}

func (s *server) RequestExecution(stream grpc.BidiStreamingServer[pb.WorkerExecutionRequest, pb.WorkerExecutionResponse]) {
	ichunk, err := stream.Recv()

	ctx := context.Background()
	ctx = context.WithValue(ctx, core.ContextIdentifierKey, &core.ExecutionIdentifier{
		RequestId:   ichunk.RequestId,
		SessionId:   ichunk.SessionId,
		ContainerId: ichunk.ContainerId,
	})

	if err == io.EOF {
		log.Panicf("No data in request stream")
	} else if err != nil {
		log.Panicf("Error receiving chunk: %v", err)
	}

	defer core.PauseContainer(ctx)

	core.PostWorkerStatus(ctx, pb.Status_STATUS_BUSY)

	stats, statsErr := core.GetContainerResources(ctx)
	if statsErr == nil {
		defer (*stats).Body.Close()
	}

	var checkpointId = ichunk.CheckpointId
	switch ichunk.Action {
	case pb.Action_ACTION_RESTART:
		checkpointId, err = core.RestartContainer(ctx, &core.ContainerConfig{
			CheckpointId: ichunk.CheckpointId,
			ResourceAllocation: core.ResourceAllocation{
				CpuAlloc: ichunk.CpuAlloc,
				GpuAlloc: ichunk.GpuAlloc,
			},
		})
	case pb.Action_ACTION_RESUME:
		err = core.ResumeContainer(ctx, &core.ResourceAllocation{
			CpuAlloc: ichunk.CpuAlloc,
			GpuAlloc: ichunk.GpuAlloc,
		})
	default:
	}

	if err != nil {
		log.Panicf("Unable to execute code due to provision error: %v", err)
	}

	conn, err := socket.GetSocketConnection(ichunk.ContainerId)
	defer socket.CloseSocketConnection(ichunk.ContainerId)
	if err != nil {
		log.Panicf("Error connecting to socket: %v", err)
	}

	for {
		chunk, err := stream.Recv()

		if err == io.EOF {
			log.Printf("Finished receiving request")
			break
		} else if err != nil {
			log.Panicf("Error receiving chunk: %v", err)
		}

		_, err = conn.Write(chunk.Payload)
		if err != nil {
			log.Panicf("Error writing to socket: %v", err)
		}
	}

	logs, err := core.GetContainerLogs(ctx)
	if err != nil {
		log.Printf("Error fetching container %s logs: %v", ichunk.ContainerId, err)
	}
	defer (*logs).Close()

	var wg sync.WaitGroup

	wg.Go(func() {
		for {
			buf := make([]byte, config.ChunkSize)
			n, err := (*logs).Read(buf)

			if err == io.EOF {
				wg.Done()
				break
			} else if err != nil {
				log.Printf("Error receiving from socket: %v", err)
				wg.Done()
			}

			stream.Send(&pb.WorkerExecutionResponse{
				WorkerId:     config.WorkerId,
				ContainerId:  ichunk.ContainerId,
				CheckpointId: checkpointId,
				CpuAlloc:     ichunk.CpuAlloc,
				GpuAlloc:     ichunk.GpuAlloc,
				Success:      true,
				Data: &pb.WorkerExecutionResponse_Logs{
					Logs: string(buf[:n]),
				},
			})
		}
	})

	for {
		buf := make([]byte, config.ChunkSize)
		n, err := conn.Read(buf)

		if err == io.EOF {
			wg.Done()
			break
		} else if err != nil {
			log.Panicf("Error reading response data: %v", err)
		}

		stream.Send(&pb.WorkerExecutionResponse{
			WorkerId:     config.WorkerId,
			ContainerId:  ichunk.ContainerId,
			CheckpointId: checkpointId,
			CpuAlloc:     ichunk.CpuAlloc,
			GpuAlloc:     ichunk.GpuAlloc,
			Success:      true,
			Data: &pb.WorkerExecutionResponse_Payload{
				Payload: buf[:n],
			},
		})
	}

	wg.Wait()

}
