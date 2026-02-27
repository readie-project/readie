package rpc

import (
	"log"
	"worker/config"
	pb "worker/proto"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

var (
	registryClient pb.RegistryServiceClient
)

func initalizeClient() *grpc.ClientConn {
	conn, err := grpc.NewClient(config.MainNodeUri, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("Failed to initialize gRPC client: %v", err)
	}

	registryClient = pb.NewRegistryServiceClient(conn)
	log.Printf("gRPC client initialized successfully")
	return conn
}

func GetRegistryClient() *pb.RegistryServiceClient {
	return &registryClient
}
