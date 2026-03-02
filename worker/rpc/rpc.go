package rpc

import (
	"context"
	"log"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
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

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	wg.Go(func() {
		log.Printf("Starting gRPC server on %s\n", addr)
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

	registryClient := clients.GetRegistryClient()
	res, err := (*registryClient).PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, WorkerUri: addr, Status: pb.Status_STATUS_READY})
	if err != nil {
		log.Fatalf("Could not connect to main node: %v", err)
	} else if !res.Updated {
		log.Fatalf("Could not update status to main node: %v", err)
	}
	log.Printf("Connected to main node with worker ID: %s", workerId)

	sig := <-sigCh
	log.Printf("Received signal: %v. Shutting down gracefully...", sig)

	ctx, cancel = context.WithTimeout(context.Background(), time.Second*5)
	defer cancel()

	res, err = (*registryClient).PostWorkerStatus(ctx, &pb.WorkerStatus{WorkerId: workerId, WorkerUri: addr, Status: pb.Status_STATUS_REMOVED})
	if err != nil {
		log.Fatalf("Could not connect to main node: %v", err)
	} else if !res.Updated {
		log.Fatalf("Could not update status to main node: %v", err)
	}
	log.Printf("Removed worker from main node with worker ID: %s", workerId)

	wg.Done()
}
