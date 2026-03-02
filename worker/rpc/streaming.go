package rpc

import (
	"context"
	"io"
	"log"
	"sync"
	"worker/config"
	"worker/core"
	pb "worker/proto"

	"google.golang.org/grpc"
)

func streamStats(ctx context.Context, wg *sync.WaitGroup, containerId string) {
	core.GetContainerResources(ctx, containerId)
	wg.Done()
}

func streamLogs(ctx context.Context, wg *sync.WaitGroup, containerId string, checkpointId string, containerConfig *core.ContainerConfig, stream *grpc.BidiStreamingServer[pb.WorkerExecutionRequest, pb.WorkerExecutionResponse]) {
	logs, err := core.GetContainerLogs(ctx, containerId)
	if err != nil {
		log.Printf("Error fetching container %s logs: %v", containerId, err)
		wg.Done()
	} else {
		defer (*logs).Close()
	}

	for {
		select {
		case <-ctx.Done():
			log.Println("Cancellation signal received for logs, exiting")
			return
		default:
		}

		buf := make([]byte, config.ChunkSize)
		n, err := (*logs).Read(buf)

		if err == io.EOF {
			break
		} else if err != nil {
			log.Printf("Error receiving from socket: %v", err)
			break
		}

		(*stream).Send(&pb.WorkerExecutionResponse{
			WorkerId:     config.WorkerId,
			ContainerId:  containerId,
			CheckpointId: checkpointId,
			CpuAlloc:     containerConfig.ResourceAllocation.CpuAlloc,
			GpuAlloc:     containerConfig.ResourceAllocation.GpuAlloc,
			Success:      true,
			Data: &pb.WorkerExecutionResponse_Logs{
				Logs: string(buf[:n]),
			},
		})
	}

	defer wg.Done()
}
