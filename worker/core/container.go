package core

import (
	"context"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"worker/clients"
	"worker/config"
	pb "worker/proto"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

type ResourceAllocation struct {
	CpuAlloc int64
	GpuAlloc int64
}

type ContainerConfig struct {
	CheckpointId       string
	ResourceAllocation ResourceAllocation
}

func generateRandomContainerName() string {
	return "random-container-name"
}

func GetContainerDir(containerId string) string {
	return filepath.Join(config.WorkerDir, containerId)
}

func startContainer(ctx context.Context, checkpointId string) error {
	cli := clients.GetDockerClient()
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	containerId := executionIdentifier.ContainerId

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
			CheckpointDir: filepath.Join(config.WorkerDir, "checkpoints"),
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

func stopContainer(ctx context.Context) error {
	cli := clients.GetDockerClient()
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)

	_, err := cli.ContainerStop(ctx, executionIdentifier.ContainerId, client.ContainerStopOptions{})
	if err != nil {
		log.Printf("Failed to stop container %s: %v", executionIdentifier.ContainerId, err)
		return err
	}
	return nil
}

func updateContainerResources(ctx context.Context, resourceAllocation *ResourceAllocation) error {
	cli := clients.GetDockerClient()
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)

	_, err := cli.ContainerUpdate(ctx, executionIdentifier.ContainerId, client.ContainerUpdateOptions{
		Resources: &container.Resources{
			Memory: resourceAllocation.CpuAlloc,
		},
	})

	if err != nil {
		log.Printf("Failed to update resources for container %s: %v", executionIdentifier.ContainerId, err)
		return err
	}
	return nil
}

func CreateAndStartContainer(ctx context.Context, containerConfig *ContainerConfig) (string, error) {
	cli := clients.GetDockerClient()
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
				Memory: containerConfig.ResourceAllocation.CpuAlloc,
			},
			Binds: []string{fmt.Sprintf("%s:%s", containerDir, config.ExecutorDir)},
		},
	})
	if createErr != nil {
		log.Printf("Failed to create docker container: %v", err)
		return "", createErr
	}
	log.Printf("Docker container %s created", containerId)

	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	ctx = context.WithValue(ctx, ContextIdentifierKey, &ExecutionIdentifier{
		RequestId:   executionIdentifier.RequestId,
		SessionId:   executionIdentifier.SessionId,
		ContainerId: containerId,
	})

	startErr := startContainer(ctx, containerConfig.CheckpointId)
	if startErr != nil {
		return "", startErr
	}

	PostWorkerStatus(ctx, pb.Status_STATUS_WAITING)
	return containerId, nil
}

func RestartContainer(ctx context.Context, containerConfig *ContainerConfig) error {
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)

	stopContainer(ctx)
	err := startContainer(ctx, containerConfig.CheckpointId)
	if err != nil {
		log.Printf("Failed to restart container %s: %v", executionIdentifier.ContainerId, err)
		return err
	}

	err = updateContainerResources(ctx, &ResourceAllocation{
		CpuAlloc: containerConfig.ResourceAllocation.CpuAlloc,
		GpuAlloc: containerConfig.ResourceAllocation.GpuAlloc,
	})
	if err != nil {
		return err
	}

	PostWorkerStatus(ctx, pb.Status_STATUS_WAITING)
	return nil
}

func PauseContainer(ctx context.Context) error {
	cli := clients.GetDockerClient()

	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	_, err := cli.ContainerPause(ctx, executionIdentifier.ContainerId, client.ContainerPauseOptions{})

	if err != nil {
		log.Printf("Failed to pause container %s: %v", executionIdentifier.ContainerId, err)
		return err
	}

	PostWorkerStatus(ctx, pb.Status_STATUS_WAITING)
	return nil
}

func ResumeContainer(ctx context.Context, resourceAllocation *ResourceAllocation) error {
	cli := clients.GetDockerClient()
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	_, err := cli.ContainerUnpause(ctx, executionIdentifier.ContainerId, client.ContainerUnpauseOptions{})
	if err != nil {
		log.Printf("Failed to resume container %s: %v", executionIdentifier.ContainerId, err)
		return err
	}

	err = updateContainerResources(ctx, &ResourceAllocation{
		CpuAlloc: resourceAllocation.CpuAlloc,
		GpuAlloc: resourceAllocation.GpuAlloc,
	})
	if err != nil {
		return err
	}

	PostWorkerStatus(ctx, pb.Status_STATUS_READY)
	return nil
}

func StopAndRemoveContainer(ctx context.Context) error {
	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	containerId := executionIdentifier.ContainerId

	defer func() {
		os.RemoveAll(GetContainerDir(containerId))
	}()

	cli := clients.GetDockerClient()
	err := stopContainer(ctx)
	if err != nil {
		return err
	}

	_, err = cli.ContainerRemove(ctx, containerId, client.ContainerRemoveOptions{})
	if err != nil {
		log.Printf("Failed to remove container %s: %v", containerId, err)
		return err
	}

	PostWorkerStatus(ctx, pb.Status_STATUS_REMOVED)
	return nil
}

func GetContainerLogs(ctx context.Context) (*client.ContainerLogsResult, error) {
	cli := clients.GetDockerClient()

	executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)
	res, err := cli.ContainerLogs(ctx, executionIdentifier.ContainerId, client.ContainerLogsOptions{
		ShowStdout: true,
		ShowStderr: true,
	})

	if err != nil {
		log.Printf("Failed to get container %s logs: %v", executionIdentifier.ContainerId, err)
		return nil, err
	}

	return &res, nil
}
