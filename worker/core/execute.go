package core

import (
	"context"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"
	pb "worker/proto"
	"worker/socket"
)

const (
	ContextIdentifierKey = "executionId"
)

type ExecutionIdentifier struct {
	RequestId   string
	SessionId   string
	ContainerId string
}

func ExecuteCode(ctx context.Context, request *pb.WorkerExecutionRequest, stream pb.ExecutionService_RequestExecutionServer) error {
	defer PauseContainer(ctx)

	stats, statsErr := GetContainerResources(ctx)
	if statsErr == nil {
		defer (*stats).Body.Close()
	}

	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)

	var err error = nil
	switch request.Action {
	case pb.Action_ACTION_RESTART:
		err = RestartContainer(ctx, &ContainerConfig{
			CheckpointId: request.CheckpointId,
			ResourceAllocation: ResourceAllocation{
				CpuAlloc: request.CpuAlloc,
				GpuAlloc: request.GpuAlloc,
			},
		})
	case pb.Action_ACTION_RESUME:
		err = ResumeContainer(ctx, &ResourceAllocation{
			CpuAlloc: request.CpuAlloc,
			GpuAlloc: request.GpuAlloc,
		})
	default:
	}

	if err != nil {
		log.Printf("Unable to execute code due to provision error: %v", err)
		return err
	}

	conn, err := socket.GetSocketConnection(executionIdentifier.ContainerId)
	defer socket.CloseSocketConnection(executionIdentifier.ContainerId)
	if err != nil {
		log.Printf("Error writing to socket: %v", err)
		return err
	}

	_, err = conn.Write([]byte("request.pkl"))
	if err != nil {
		log.Printf("Error writing to socket: %v", err)
		return err
	}

	logs, err := GetContainerLogs(ctx)
	if err != nil {
		log.Printf("Error fetching container %s logs: %v", executionIdentifier.ContainerId, err)
		return err
	}
	defer (*logs).Close()

	var wg sync.WaitGroup

	wg.Go(func() {
		for {
			buf := make([]byte, 1024)
			n, err := (*logs).Read(buf)

			if err == io.EOF {
				wg.Done()
				break
			} else if err != nil {
				log.Printf("Error receiving from socket: %v", err)
				wg.Done()
			}

			stream.Send(&pb.WorkerExecutionResponse{
				ContainerId: request.ContainerId,
				Success:     true,
				Logs:        string(buf[:n]),
				Payload:     nil,
			})
		}
	})

	var fileName string
	buf := make([]byte, 1024)
	n, err := conn.Read(buf)
	if err == io.EOF {
		fileName = ""
		wg.Done()
	} else if err != nil {
		log.Printf("Error receiving from socket: %v", err)
		return err
	}
	fileName = string(buf[:n])

	wg.Wait()

	if fileName == "" {
		return nil
	}

	file, err := os.Open(filepath.Join(GetContainerDir(executionIdentifier.ContainerId), fileName))
	if err != nil {
		log.Printf("Error opening response file: %v", err)
	}
	defer file.Close()

	chunkSize := 1024 * 1024 // 1MB chunks
	for {
		buf := make([]byte, chunkSize)
		_, err := file.Read(buf)

		if err == io.EOF {
			break
		} else if err != nil {
			log.Printf("Error reading response file: %v", err)
		}

		stream.Send(&pb.WorkerExecutionResponse{
			ContainerId: request.ContainerId,
			Success:     true,
			Logs:        "",
			Payload:     buf[:n],
		})
	}

	return nil
}
