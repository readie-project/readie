package docker

import (
	pb "worker/proto"

	"github.com/moby/moby/client"
)

var (
	cli      *client.Client
	registry *pb.RegistryServiceClient
)

func InitializeDockerClientWithRegistry(registryService *pb.RegistryServiceClient) {
	apiClient, err := client.New(client.FromEnv)
	if err != nil {
		panic(err)
	}
	cli = apiClient
	registry = registryService
}

func GetDockerClient() *client.Client {
	return cli
}

func GetRegistryClient() *pb.RegistryServiceClient {
	return registry
}
