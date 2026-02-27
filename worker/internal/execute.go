package worker

import (
	"context"
	"fmt"
	"io"
	"log"
	"worker/internal/docker"
	"worker/internal/socket"
	pb "worker/proto"
)

func getCheckPointAndImageForExecution(request *pb.WorkerExecutionRequest) (string, string) {
	return "", "test-agent"
}

func PrepareExecutor(ctx context.Context, request *pb.WorkerExecutionRequest) (string, string) {
	checkpointId, image := getCheckPointAndImageForExecution(request)
	if request.ContainerId == nil {
		containerId, containerName, _ := docker.StartContainer(ctx, &docker.ContainerConfig{
			Image:        image,
			CheckpointId: checkpointId,
			CPU:          512 * 1024 * 1024, // 512MB
		})
		request.ContainerId = &containerId
		return *request.ContainerId, containerName
	} else {
		docker.ResumeContainer(ctx, *request.ContainerId)
		return *request.ContainerId, *request.ContainerId
	}
}

func ExecuteCode(ctx context.Context, request *pb.WorkerExecutionRequest, containerName string, fileName string, stream pb.ExecutionService_RequestExecutionServer) error {
	fmt.Printf("Started container with ID: %s\n", containerName)
	// defer docker.StopContainer(ctx, *request.ContainerId)

	conn := socket.GetSocketConnection(containerName)
	defer socket.CloseSocketConnection(containerName)

	fmt.Printf("Connected! Sending message..., %s\n", fileName)
	n, err := conn.Write([]byte(fileName))
	if err != nil {
		fmt.Printf("Error writing to socket: %v\n", err)
	}

	for {
		buf := make([]byte, 1024)
		n, err = conn.Read(buf)
		fmt.Printf("Python responded: %s\n", string(buf[:n]))

		if err == io.EOF {
			log.Println("Unix socket closed by Python. Moving on...")
			// Don't return the error to gRPC unless this was mandatory
			break
		} else if err != nil {
			log.Printf("Actual Socket Error: %v", err)
			return err
		}

		stream.Send(&pb.WorkerExecutionResponse{
			RequestId:   request.RequestId,
			SessionId:   request.SessionId,
			WorkerId:    request.WorkerId,
			ContainerId: "x",
			Success:     true,
			Stdout:      string(buf[:n]),
			Stderr:      "",
		})
	}

	return nil
}
