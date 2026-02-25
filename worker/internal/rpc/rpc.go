package rpc

import (
	"context"
	"io"
	"log"
	"net"
	"sync"
	"time"
	"worker/internal/docker"
	pb "worker/proto"

	"google.golang.org/grpc"
)

type server struct {
	pb.UnimplementedExecutionServiceServer
}

func (s *server) RequestExecution(stream pb.ExecutionService_RequestExecutionServer) error {
	req, err := stream.Recv()
	log.Printf("Received execution request: %s\n", req, err)
	if err == io.EOF {
		return nil
	}
	if err != nil {
		return err
	}

	log.Printf("Received execution request: %s\n", req.RequestId)

	err = stream.Send(&pb.WorkerExecutionResponse{
		RequestId: req.RequestId,
		Stdout:    "Job Started...",
	})

	return err
}

const (
	grpcServerHost = "localhost:50052"
)

func StartServer() {
	conn, registryClient := initalizeClient()
	defer conn.Close()

	var wg sync.WaitGroup

	wg.Go(func() {
		lis, err := net.Listen("tcp", grpcServerHost)
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
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	r, err := registryClient.PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, Status: "active"})
	if err != nil {
		log.Fatalf("could not greet: %v", err)
	}
	log.Printf("Greeting: %s", r.WorkerId)

	docker.InitializeDockerClientWithRegistry(&registryClient)

	wg.Wait()
}
