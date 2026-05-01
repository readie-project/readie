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
	"worker/core"
	pb "worker/proto"

	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/reflection"
)

func StartServer() {
	var wg sync.WaitGroup
	workerId := config.WorkerId
	addr := config.WorkerUri

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)

	log.Printf("Starting gRPC server on %s\n", addr)
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("Failed to listen gRPC server: %v", err)
	}
	log.Printf("gRPC server listening at %v", lis.Addr())

	s := grpc.NewServer()
	pb.RegisterExecutionServiceServer(s, &server{})
	reflection.Register(s)

	healthServer := health.NewServer()
	grpc_health_v1.RegisterHealthServer(s, healthServer)
	healthServer.SetServingStatus("", grpc_health_v1.HealthCheckResponse_SERVING)
	log.Printf("Health check set to serving")

	wg.Add(1)
	go (func() {
		if err := s.Serve(lis); err != nil {
			log.Fatalf("Failed to start gRPC server: %v", err)
		}
	})()

	conn := clients.InitalizeRPCClient()
	defer conn.Close()

	clients.InitializeDockerClient()
	defer clients.CloseDockerClient()
	defer core.ContainerCleanup(context.Background())

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()

	updated := core.PostWorkerStatus(ctx, pb.Status_STATUS_READY)
	if !updated {
		log.Fatalf("Could not connect to router and register worker")
	}
	log.Printf("Connected to router with worker ID: %s", workerId)
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second*5)
		defer cancel()

		updated = core.PostWorkerStatus(ctx, pb.Status_STATUS_REMOVED)
		if !updated {
			log.Fatalf("Could not connect to router and remove worker")
		}
		log.Printf("Removed worker from router with worker ID: %s", workerId)
	}()

	defer wg.Done()

	// wg.Wait()
	sig := <-sigCh
	log.Printf("Received signal: %v. Shutting down gracefully...", sig)
}
