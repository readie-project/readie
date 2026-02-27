package rpc

import (
	"context"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"worker/config"
	"worker/core"
	"worker/docker"
	pb "worker/proto"
)

type server struct {
	pb.UnimplementedExecutionServiceServer
}

func (s *server) RequestProvision(request *pb.ExecutorProvisionRequest) pb.ExecutorProvisionResponse {
	ctx := context.Background()
	containerId, err := docker.CreateAndStartContainer(ctx, &docker.ContainerConfig{
		CheckpointId: request.CheckpointId,
		CpuAlloc:     request.CpuAlloc,
		GpuAlloc:     request.GpuAlloc,
	})

	if err != nil {
		log.Panicf("Failed to provision container: %v", err)
	}
	return pb.ExecutorProvisionResponse{
		ContainerId: containerId,
		CpuAlloc:    request.CpuAlloc,
		GpuAlloc:    request.GpuAlloc,
	}
}

func (s *server) RequestExecution(stream pb.ExecutionService_RequestExecutionServer) error {
	var fileSize int64
	var prevChunk *pb.WorkerExecutionRequest
	var file *os.File
	var fileName string
	var containerId string
	var containerName string

	ctx := context.Background()

	for {
		chunk, err := stream.Recv()
		log.Printf("Received execution request: %v\n", chunk)

		if err == io.EOF {
			log.Printf("Finished receiving file. Total size: %d bytes", fileSize)
			file.Close()
			if fileSize > 0 {
				core.ExecuteCode(ctx, prevChunk, containerName, fileName, stream)
			}
			return nil
		} else if err != nil {
			log.Printf("Error receiving chunk: %v\n", err)
			return err
		}

		if fileName == "" {
			containerId, containerName = "21930809238420", "c-112949" // worker.PrepareExecutor(ctx, chunk)
			log.Printf("CID: %s\n", containerId)
			dir := filepath.Join(config.WorkerSocketDir, containerName)
			os.MkdirAll(dir, 0777)
			fileName = fmt.Sprintf("%s.pkl", chunk.RequestId)
			filePath := filepath.Join(dir, fileName)
			file, _ = os.Create(filePath)
		}

		prevChunk = chunk

		n, err := file.Write(chunk.Payload)
		if err != nil {
			log.Printf("Error writing chunk: %v\n", err)
			return err
		}
		fileSize += int64(n)
	}
}
