/* IMP: Only gRPC functions and operations should be panicking */
package rpc

import (
	"context"
	"io"
	"log"
	"os"
	"path/filepath"
	"worker/core"
	pb "worker/proto"

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

	containerId, err := core.CreateAndStartContainer(ctx, &core.ContainerConfig{
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
		ContainerId: containerId,
		CpuAlloc:    request.CpuAlloc,
		GpuAlloc:    request.GpuAlloc,
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

	dir := core.GetContainerDir(ichunk.ContainerId)

	err = os.MkdirAll(dir, 0777)
	if err != nil {
		log.Panicf("Could not create directory %s for request: %v", dir, err)
	}

	filePath := filepath.Join(dir, "request.pkl")
	file, err := os.Create(filePath)
	if err != nil {
		log.Panicf("Could not create request file at %s for request: %v", filePath, err)
	}
	log.Printf("Created request file at %s", filePath)

	var fileSize int
	n, err := file.Write(ichunk.Payload)
	if err != nil {
		log.Panicf("Error writing chunk to request file %s: %v", filePath, err)
	}
	fileSize += n

	for {
		chunk, err := stream.Recv()

		if err == io.EOF {
			log.Printf("Finished receiving request. Total size: %d bytes", fileSize)
			file.Close()
			if fileSize > 0 {
				executeErr := core.ExecuteCode(ctx, ichunk, stream)
				if executeErr != nil {
					log.Panicf("Error during execution: %v", executeErr)
				}
			}
			break
		} else if err != nil {
			log.Panicf("Error receiving chunk: %v", err)
		}

		n, err := file.Write(chunk.Payload)
		if err != nil {
			log.Panicf("Error writing chunk to request file %s: %v", filePath, err)
		}
		fileSize += n
	}
}
