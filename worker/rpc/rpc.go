package rpc

import (
	"context"
	"log"
	"net"
	"sync"
	"time"
	"worker/clients"
	"worker/config"
	pb "worker/proto"

	"google.golang.org/grpc"
)

func StartServer() {
	conn := clients.InitalizeRPCClient()
	defer conn.Close()

	clients.InitializeDockerClient()
	defer clients.CloseDockerClient()

	var wg sync.WaitGroup
	addr := config.WorkerNodeUri
	workerId := config.WorkerId

	wg.Go(func() {
		log.Printf("Starting gRPC server on %s\n", addr)
		lis, err := net.Listen("tcp", addr)
		if err != nil {
			log.Fatalf("Failed to listen gRPC server: %v", err)
		}

		s := grpc.NewServer()
		// pb.RegisterExecutionServiceServer(s, &server{})
		log.Printf("gRPC server listening at %v", lis.Addr())

		if err := s.Serve(lis); err != nil {
			log.Fatalf("Failed to start gRPC server: %v", err)
		}
	})

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	registryClient := clients.GetRegistryClient()
	res, err := (*registryClient).PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, WorkerUri: addr, Status: pb.Status_STATUS_READY})
	if err != nil {
		log.Fatalf("Could not connect to main node: %v", err)
	} else if !res.Updated {
		log.Fatalf("Could not update status to main node: %v", err)
	}
	log.Printf("Connected to main node with worker ID: %s", workerId)

	wg.Wait()
}
