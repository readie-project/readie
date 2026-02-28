package clients

import (
	"log"

	"github.com/moby/moby/client"
)

var (
	dockerClient *client.Client
)

func InitializeDockerClient() {
	apiClient, err := client.New(client.FromEnv)
	if err != nil {
		log.Fatalf("Failed to initialize docker client: %v", err)
	}
	log.Printf("Docker client initialized successfully")
	dockerClient = apiClient
}

func CloseDockerClient() {
	dockerClient.Close()
	dockerClient = nil
}

func GetDockerClient() *client.Client {
	return dockerClient
}
