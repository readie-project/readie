package docker

import (
	"context"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

const (
	checkpoint_dir = "/tmp/checkpoints"
)

type ContainerConfig struct {
	Image        string
	CheckpointID string
	CPU          int64
	GPU          int64
}

func StartContainer(ctx context.Context, containerConfig *ContainerConfig) (string, error) {
	cli := GetDockerClient()
	createResp, createErr := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Config: &container.Config{
			Image:           containerConfig.Image,
			NetworkDisabled: true,
		},
		HostConfig: &container.HostConfig{
			Runtime: "runsc",
			Resources: container.Resources{
				Memory: containerConfig.CPU,
			},
		},
	})

	if createResp.ID == "" {
		return "", createErr
	}

	containerID := createResp.ID

	_, startErr := cli.ContainerStart(ctx, containerID, client.ContainerStartOptions{
		CheckpointID:  containerConfig.CheckpointID,
		CheckpointDir: checkpoint_dir,
	})

	if startErr != nil {
		return "", startErr
	}

	return containerID, nil
}

func PauseContainer(ctx context.Context, containerID string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerPause(ctx, containerID, client.ContainerPauseOptions{})
	return err
}

func ResumeContainer(ctx context.Context, containerID string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerUnpause(ctx, containerID, client.ContainerUnpauseOptions{})
	return err
}

func StopContainer(ctx context.Context, containerID string) error {
	cli := GetDockerClient()
	_, err := cli.ContainerStop(ctx, containerID, client.ContainerStopOptions{})
	if err != nil {
		return err
	}
	_, err = cli.ContainerRemove(ctx, containerID, client.ContainerRemoveOptions{})
	return err
}