package docker

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"worker/config"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

type ContainerConfig struct {
	CheckpointId string
	CpuAlloc     int64
	GpuAlloc     int64
}

func generateRandomContainerName() string {
	return "random-container-name"
}

func GetContainerDir(containerId string) string {
	return filepath.Join(config.WorkerDir, containerId)
}

func getCheckpointsDir(containerId string) string {
	return filepath.Join(config.WorkerDir, "checkpoints")
}

func startContainer(ctx context.Context, containerId string, checkpointId string) error {
	cli := getDockerClient()
	if checkpointId == "" {
		// Start a standard container
		_, startErr := cli.ContainerStart(ctx, containerId, client.ContainerStartOptions{})

		if startErr != nil {
			log.Printf("Failed to start standard docker container %s: %v", containerId, startErr)
			return startErr
		}
		return nil
	} else {
		// Start a container with checkpoint
		_, startErr := cli.ContainerStart(ctx, containerId, client.ContainerStartOptions{
			CheckpointID:  checkpointId,
			CheckpointDir: getCheckpointsDir(containerId),
		})

		if startErr != nil {
			log.Printf("Failed to start docker container %s with checkpoint %s: %v", containerId, checkpointId, startErr)
			// Try starting a standard container
			_, startErr = cli.ContainerStart(ctx, containerId, client.ContainerStartOptions{})
			if startErr != nil {
				log.Printf("Failed to start standard docker container %s: %v", containerId, startErr)
				return startErr
			}
		}
		return nil
	}
}

func stopContainer(ctx context.Context, containerId string) error {
	cli := getDockerClient()
	_, err := cli.ContainerStop(ctx, containerId, client.ContainerStopOptions{})
	if err != nil {
		log.Printf("Failed to stop container %s: %v", containerId, err)
		return err
	}
	return nil
}

func CreateAndStartContainer(ctx context.Context, containerConfig *ContainerConfig) (string, error) {
	cli := getDockerClient()
	containerId := generateRandomContainerName()
	containerDir := GetContainerDir(containerId)

	err := os.RemoveAll(containerDir)
	err = os.MkdirAll(containerDir, 0777)
	if err != nil {
		return "", err
	}

	_, createErr := cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: containerId,
		Config: &container.Config{
			Image:           config.ExecutorImage,
			NetworkDisabled: true,
			Env:             []string{fmt.Sprintf("EXECUTOR_DIR=%s", config.ExecutorDir)},
		},
		HostConfig: &container.HostConfig{
			// Runtime: "runsc",
			Resources: container.Resources{
				Memory: containerConfig.CpuAlloc,
			},
			Binds: []string{fmt.Sprintf("%s:%s", containerDir, config.ExecutorDir)},
		},
	})
	if createErr != nil {
		log.Printf("Failed to create docker container: %v", err)
		return "", createErr
	}
	log.Printf("Docker container %s created", containerId)

	startErr := startContainer(ctx, containerId, containerConfig.CheckpointId)
	if startErr != nil {
		return "", startErr
	}

	return containerId, nil
}

func RestartContainer(ctx context.Context, containerId string, checkpointId string) error {
	stopContainer(ctx, containerId)
	err := startContainer(ctx, containerId, checkpointId)

	if err != nil {
		log.Printf("Failed to restart container %s: %v", containerId, err)
		return err
	}
	return nil
}

func PauseContainer(ctx context.Context, containerId string) error {
	cli := getDockerClient()
	_, err := cli.ContainerPause(ctx, containerId, client.ContainerPauseOptions{})

	if err != nil {
		log.Printf("Failed to pause container %s: %v", containerId, err)
		return err
	}
	return nil
}

func ResumeContainer(ctx context.Context, containerId string) error {
	cli := getDockerClient()
	_, err := cli.ContainerUnpause(ctx, containerId, client.ContainerUnpauseOptions{})

	if err != nil {
		log.Printf("Failed to resume container %s: %v", containerId, err)
		return err
	}
	return nil
}

func StopAndRemoveContainer(ctx context.Context, containerId string) error {
	defer func() {
		os.RemoveAll(GetContainerDir(containerId))
	}()

	cli := getDockerClient()
	err := stopContainer(ctx, containerId)
	if err != nil {
		return err
	}

	_, err = cli.ContainerRemove(ctx, containerId, client.ContainerRemoveOptions{})
	if err != nil {
		log.Printf("Failed to remove container %s: %v", containerId, err)
		return err
	}
	return nil
}
