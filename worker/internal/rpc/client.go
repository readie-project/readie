package rpc

import (
	"log"
	"worker/internal/config"
	pb "worker/proto"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	workerId = "world"
)

func initalizeClient() (*grpc.ClientConn, pb.RegistryServiceClient) {
	conn, err := grpc.NewClient(config.MainNodeUri, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("did not connect: %v", err)
	}

	registryClient := pb.NewRegistryServiceClient(conn)

	return conn, registryClient
}
