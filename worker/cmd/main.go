package main

import (
	"context"
	"fmt"
	"worker/internal/docker"
	"worker/internal/socket"

	"github.com/joho/godotenv"
)

func main() {
	err := godotenv.Load()
	if err != nil {
		fmt.Println("Error loading .env file:", err)
	}

	// rpc.StartServer()
	docker.InitializeDockerClientWithRegistry()

	containerId, containerName, _ := docker.StartContainer(context.Background(), &docker.ContainerConfig{
		Image: "test-agent",
		// CheckpointId: "checkpoint1",
		CPU: 512 * 1024 * 1024, // 512MB
		GPU: 0,
	})
	fmt.Printf("Started container with ID: %s\n", containerName)
	defer docker.StopContainer(context.Background(), containerId)

	conn := socket.GetSocketConnection(containerName)
	defer socket.CloseSocketConnection(containerName)

	fmt.Println("Connected! Sending message...")
	conn.Write([]byte("Hello from Go Client"))

	buf := make([]byte, 1024)
	n, _ := conn.Read(buf)
	fmt.Printf("Python responded: %s\n", string(buf[:n]))
}
