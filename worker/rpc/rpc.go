package rpc

import (
	"context"
	"fmt"
	"log"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
	"worker/clients"
	"worker/config"
	"worker/core"
	pb "worker/proto"

	"google.golang.org/grpc"
)

func StartServer() {
	var wg sync.WaitGroup
	addr := fmt.Sprintf(":%s", config.Port)
	workerId := config.WorkerId

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	log.Printf("Starting gRPC server on %s\n", addr)
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("Failed to listen gRPC server: %v", err)
	}

	s := grpc.NewServer()
	pb.RegisterExecutionServiceServer(s, &server{})
	log.Printf("gRPC server listening at %v", lis.Addr())
	workerUri := lis.Addr().String()

	wg.Go(func() {
		if err := s.Serve(lis); err != nil {
			log.Fatalf("Failed to start gRPC server: %v", err)
		}
	})

	conn := clients.InitalizeRPCClient()
	defer conn.Close()

	clients.InitializeDockerClient()
	defer core.ContainerCleanup(context.Background())
	defer clients.CloseDockerClient()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	updated := core.PostWorkerStatus(ctx, workerUri, pb.Status_STATUS_READY)
	if !updated {
		log.Fatalf("Could not connect to main node and register worker")
	}
	log.Printf("Connected to main node with worker ID: %s", workerId)

	// w.Wait()
	sig := <-sigCh
	log.Printf("Received signal: %v. Shutting down gracefully...", sig)

	ctx, cancel = context.WithTimeout(context.Background(), time.Second*5)
	defer cancel()

	updated = core.PostWorkerStatus(ctx, workerUri, pb.Status_STATUS_REMOVED)
	if !updated {
		log.Fatalf("Could not connect to main node and remove worker")
	}
	log.Printf("Removed worker from main node with worker ID: %s", workerId)

	core.ContainerCleanup(ctx)

	wg.Done()
}
