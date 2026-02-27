package rpc

import (
	"context"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"sync"
	"worker"
	"worker/internal/config"
	"worker/internal/docker"
	pb "worker/proto"

	"google.golang.org/grpc"
)

type server struct {
	pb.UnimplementedExecutionServiceServer
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
				worker.ExecuteCode(ctx, prevChunk, containerName, fileName, stream)
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

func StartServer() {
	conn, registryClient := initalizeClient()
	defer conn.Close()

	var wg sync.WaitGroup

	wg.Go(func() {
		fmt.Printf("Starting gRPC server on %s\n", os.Getenv("WORKER_NODE_URI"))
		lis, err := net.Listen("tcp", config.WorkerNodeUri)
		if err != nil {
			log.Fatalf("failed to listen: %v", err)
		}
		s := grpc.NewServer()
		pb.RegisterExecutionServiceServer(s, &server{})
		log.Printf("server listening at %v", lis.Addr())
		if err := s.Serve(lis); err != nil {
			log.Fatalf("failed to serve: %v", err)
		}
	})

	// Contact the server and print out its response.
	// ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	// defer cancel()
	// r, err := registryClient.PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, Status: "active"})
	// if err != nil {
	// 	log.Fatalf("could not greet: %v", err)
	// }
	// log.Printf("Greeting: %s", r.WorkerId)

	docker.InitializeDockerClientWithRegistry(&registryClient)

	wg.Wait()
}
