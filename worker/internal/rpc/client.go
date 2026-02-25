package rpc

import (
	"log"
	pb "worker/proto"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	workerId = "world"
	grpcHost = "localhost:50051"
)

func initalizeClient() (*grpc.ClientConn, pb.RegistryServiceClient) {
	conn, err := grpc.NewClient(grpcHost, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatalf("did not connect: %v", err)
	}

	registryClient := pb.NewRegistryServiceClient(conn)

	return conn, registryClient
}
