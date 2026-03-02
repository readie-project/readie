/* IMP: Only gRPC functions and operations should be panicking if required */
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

func (s *server) RequestExecution(stream grpc.BidiStreamingServer[pb.WorkerExecutionRequest, pb.WorkerExecutionResponse]) error {
	log.Printf("Received execution request")
	ichunk, err := stream.Recv()

	if err == io.EOF {
		log.Printf("No data in request stream")
		return nil
	} else if err != nil {
		log.Printf("Error receiving chunk: %v", err)
		return err
	}

	ctx := context.Background()
	ctx = context.WithValue(ctx, core.ContextIdentifierKey, &core.ExecutionIdentifier{
		RequestId: ichunk.RequestId,
		SessionId: ichunk.SessionId,
	})
	containerId := *ichunk.ContainerId
	checkpointId := ichunk.CheckpointId

	if containerId == "" {
		containerId, checkpointId, err = core.CreateAndStartContainer(ctx, &core.ContainerConfig{
			CheckpointId: checkpointId,
			ResourceAllocation: core.ResourceAllocation{
				CpuAlloc: ichunk.CpuAlloc,
				GpuAlloc: ichunk.GpuAlloc,
			},
		})

		if err != nil {
			log.Printf("Failed to provision container: %v", err)
			return err
		}
	} else {
		err = core.ResumeContainer(ctx, containerId, &core.ResourceAllocation{
			CpuAlloc: ichunk.CpuAlloc,
			GpuAlloc: ichunk.GpuAlloc,
		})

		if err != nil {
			log.Printf("Failed to resume container: %v", err)
			return err
		}
	}

	defer func() {
		if r := recover(); r != nil {
			core.StopAndRemoveContainer(ctx, containerId)
		} else {
			core.PauseContainer(ctx, containerId)
		}
	}()

	containerConfig, err := core.InspectContainer(ctx, containerId, checkpointId)
	if err != nil {
		log.Printf("Error inspecting container %s: %v", containerId, err)
		return err
	}
	log.Printf("Inspected container successfully")

	core.PostContainerStatus(ctx, containerId, pb.Status_STATUS_BUSY)

	var wg sync.WaitGroup
	streamCtx, cancelStream := context.WithCancel(ctx)
	defer cancelStream()

	wg.Add(1)
	go streamStats(streamCtx, &wg, containerId)

	conn, err := socket.GetSocketConnection(containerId)
	defer socket.CloseSocketConnection(containerId)
	if err != nil {
		log.Printf("Error connecting to socket: %v", err)
		return err
	}

	_, err = conn.Write(ichunk.Payload)
	if err != nil {
		log.Printf("Error writing to socket: %v", err)
		return err
	}

	for {
		chunk, err := stream.Recv()

		if err == io.EOF {
			log.Printf("Finished receiving request")
			conn.Write([]byte("EOF"))
			break
		} else if err != nil {
			log.Printf("Error receiving chunk: %v", err)
			return err
		}

		_, err = conn.Write(chunk.Payload)
		if err != nil {
			log.Printf("Error writing to socket: %v", err)
			return err
		}
	}

	wg.Add(1)
	go streamLogs(streamCtx, &wg, containerId, checkpointId, containerConfig, &stream)

	for {
		buf := make([]byte, config.ChunkSize)
		n, err := conn.Read(buf)

		if err == io.EOF {
			break
		} else if err != nil {
			log.Printf("Error reading response data: %v", err)
			return err
		}

		stream.Send(&pb.WorkerExecutionResponse{
			WorkerId:     config.WorkerId,
			ContainerId:  containerId,
			CheckpointId: checkpointId,
			CpuAlloc:     containerConfig.ResourceAllocation.CpuAlloc,
			GpuAlloc:     containerConfig.ResourceAllocation.GpuAlloc,
			Success:      true,
			Data: &pb.WorkerExecutionResponse_Payload{
				Payload: buf[:n],
			},
		})
	}

	return nil
}
