package rpc

import (
	"context"
	"fmt"
	"log"
	"net"
	"sync"
	"time"
	"worker/config"
	"worker/docker"
	pb "worker/proto"

	"google.golang.org/grpc"
)

func StartServer() {
	conn := initalizeClient()
	defer conn.Close()

	docker.InitializeDockerClient()

	var wg sync.WaitGroup
	addr := config.WorkerNodeUri
	workerId := config.WorkerId

	wg.Go(func() {
		fmt.Printf("Starting gRPC server on %s\n", addr)
		lis, err := net.Listen("tcp", addr)
		if err != nil {
			log.Fatalf("Failed to listen gRPC server: %v", err)
		}

		s := grpc.NewServer()
		pb.RegisterExecutionServiceServer(s, &server{})
		log.Printf("gRPC server listening at %v", lis.Addr())

		if err := s.Serve(lis); err != nil {
			log.Fatalf("Failed to start gRPC server: %v", err)
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	res, err := registryClient.PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, WorkerUri: addr, Status: pb.Status_READY})
	if err != nil {
		log.Fatalf("Could not connect to main node: %v", err)
	} else if !res.Updated {
		log.Fatalf("Could not update status to main node: %v", err)
	}
	log.Printf("Connected to main node with worker ID: %s", workerId)

	wg.Wait()
}
