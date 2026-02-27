package core

import (
	"context"
	"io"
	"log"
	"worker/docker"
	pb "worker/proto"
	"worker/socket"
)

func ExecuteCode(request *pb.WorkerExecutionRequest, stream pb.ExecutionService_RequestExecutionServer) error {
	ctx := context.Background()
	defer docker.PauseContainer(ctx, request.ContainerId)

	var err error = nil
	switch request.Action {
	case pb.Action_ACTION_RESTART:
		err = docker.RestartContainer(ctx, request.ContainerId, &docker.ContainerConfig{
			CheckpointId: request.CheckpointId,
			ResourceAllocation: docker.ResourceAllocation{
				CpuAlloc: request.CpuAlloc,
				GpuAlloc: request.GpuAlloc,
			},
		})
	case pb.Action_ACTION_RESUME:
		err = docker.ResumeContainer(ctx, request.ContainerId, &docker.ResourceAllocation{
			CpuAlloc: request.CpuAlloc,
			GpuAlloc: request.GpuAlloc,
		})
	default:
	}

	if err != nil {
		log.Printf("Unable to execute code due to provision error: %v", err)
		return err
	}

	conn, err := socket.GetSocketConnection(request.ContainerId)
	defer socket.CloseSocketConnection(request.ContainerId)
	if err != nil {
		log.Printf("Error writing to socket: %v", err)
		return err
	}

	n, err := conn.Write([]byte("request.pkl"))
	if err != nil {
		log.Printf("Error writing to socket: %v", err)
		return err
	}

	for {
		buf := make([]byte, 1024)
		n, err = conn.Read(buf)

		if err == io.EOF {
			break
		} else if err != nil {
			log.Printf("Error receiving from socket: %v", err)
			return err
		}

		stream.Send(&pb.WorkerExecutionResponse{
			ContainerId: request.ContainerId,
			Success:     true,
			Stdout:      string(buf[:n]),
			Stderr:      "",
		})
	}

	return nil
}
