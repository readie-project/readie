package docker

import (
	"context"
	"fmt"
	"time"
	"worker/internal/config"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

const (
	checkpoint_dir = "/tmp/checkpoints"
)

type ContainerConfig struct {
	Image string
	// CheckpointId string
	CPU int64
	GPU int64
}

func StartContainer(ctx context.Context, containerConfig *ContainerConfig) (string, string, error) {
	cli := GetDockerClient()
	containerName := fmt.Sprintf("c-%d", time.Now().UnixNano())[:8]

	createResp, createErr := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: containerName,
		Config: &container.Config{
			Image:           containerConfig.Image,
			NetworkDisabled: true,
			Env:             []string{fmt.Sprintf("CONTAINER_ID=%s", containerName), fmt.Sprintf("AGENT_SOCKET_DIR=%s", config.AgentSocketDir)},
		},
		HostConfig: &container.HostConfig{
			// Runtime: "runsc",
			Resources: container.Resources{
				// Memory: containerConfig.CPU,
			},
			Binds: []string{fmt.Sprintf("%s:%s", config.WorkerSocketDir, config.AgentSocketDir)},
		},
	})
	fmt.Printf("Docker container created %v", createResp)

	if createResp.ID == "" {
		fmt.Printf("Docker container error %v", createErr)
		return "", "", createErr
	}

	containerId := createResp.ID

	_, startErr := cli.ContainerStart(ctx, containerId, client.ContainerStartOptions{
		// CheckpointID:  containerConfig.CheckpointId,
		// CheckpointDir: checkpoint_dir,
	})

	if startErr != nil {
		return "", "", startErr
	}

	return containerId, containerName, nil
}

func PauseContainer(ctx context.Context, containerId string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerPause(ctx, containerId, client.ContainerPauseOptions{})
	return err
}

func ResumeContainer(ctx context.Context, containerId string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerUnpause(ctx, containerId, client.ContainerUnpauseOptions{})
	return err
}

func StopContainer(ctx context.Context, containerId string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerStop(ctx, containerId, client.ContainerStopOptions{})
	if err != nil {
		return err
	}
	_, err = cli.ContainerRemove(ctx, containerId, client.ContainerRemoveOptions{})
	return err
}
