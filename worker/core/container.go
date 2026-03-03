package core

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"time"
	"worker/clients"
	"worker/config"
	pb "worker/proto"

	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

const (
	ContextIdentifierKey = "executionId"
)

type ExecutionIdentifier struct {
	RequestId string
	SessionId string
}

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

func startContainer(ctx context.Context, containerId string, checkpointId string) (string, error) {
	cli := clients.GetDockerClient()
	// executionIdentifier := ctx.Value(ContextIdentifierKey).(*ExecutionIdentifier)

	if checkpointId == "" {
		// Start a standard container
		_, startErr := cli.ContainerStart(ctx, containerId, client.ContainerStartOptions{})

		if startErr != nil {
			log.Printf("Failed to start standard docker container %s: %v", containerId, startErr)
			return "", startErr
		}
		return "", nil
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
				return "", startErr
			}
			return "", nil
		}
		return checkpointId, nil
	}
}

func stopContainer(ctx context.Context, containerId string) error {
	cli := clients.GetDockerClient()

	_, err := cli.ContainerStop(ctx, containerId, client.ContainerStopOptions{})
	if err != nil {
		log.Printf("Failed to stop container %s: %v", containerId, err)
		return err
	}
	return nil
}

func updateContainerResources(ctx context.Context, containerId string, resourceAllocation *ResourceAllocation) error {
	cli := clients.GetDockerClient()

	_, err := cli.ContainerUpdate(ctx, containerId, client.ContainerUpdateOptions{
		Resources: &container.Resources{
			Memory: resourceAllocation.CpuAlloc,
		},
	})

	if err != nil {
		log.Printf("Failed to update resources for container %s: %v", containerId, err)
		return err
	}
	return nil
}

func CreateAndStartContainer(ctx context.Context, containerConfig *ContainerConfig) (string, string, error) {
	cli := clients.GetDockerClient()
	containerId := generateRandomContainerName()
	containerDir := GetContainerDir(containerId)

	err := os.RemoveAll(containerDir)
	err = os.MkdirAll(containerDir, 0777)
	if err != nil {
		return "", "", err
	}

	pidsLimit := int64(100)

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
				Memory:    containerConfig.ResourceAllocation.CpuAlloc,
				CPUQuota:  50000,
				PidsLimit: &pidsLimit,
			},
			Binds: []string{fmt.Sprintf("%s:%s", containerDir, config.ExecutorDir)},
		},
	})
	if createErr != nil {
		log.Printf("Failed to create docker container: %v", createErr)
		return "", "", createErr
	}
	log.Printf("Docker container %s created", containerId)

	checkpointId, startErr := startContainer(ctx, containerId, containerConfig.CheckpointId)
	if startErr != nil {
		return "", "", startErr
	}

	return containerId, checkpointId, nil
}

func PauseContainer(ctx context.Context, containerId string) error {
	cli := clients.GetDockerClient()

	_, err := cli.ContainerPause(ctx, containerId, client.ContainerPauseOptions{})

	if err != nil {
		log.Printf("Failed to pause container %s: %v", containerId, err)
		PostContainerStatus(ctx, containerId, pb.Status_STATUS_ERROR)
		return err
	}
	log.Printf("Docker container %s paused", containerId)

	PostContainerStatus(ctx, containerId, pb.Status_STATUS_READY)
	return nil
}

func ResumeContainer(ctx context.Context, containerId string, resourceAllocation *ResourceAllocation) error {
	cli := clients.GetDockerClient()

	_, err := cli.ContainerUnpause(ctx, containerId, client.ContainerUnpauseOptions{})
	if err != nil {
		log.Printf("Failed to resume container %s: %v", containerId, err)
		PostContainerStatus(ctx, containerId, pb.Status_STATUS_ERROR)
		return err
	}
	log.Printf("Docker container %s resumed", containerId)

	err = updateContainerResources(ctx, containerId, &ResourceAllocation{
		CpuAlloc: resourceAllocation.CpuAlloc,
		GpuAlloc: resourceAllocation.GpuAlloc,
	})
	if err != nil {
		return err
	}

	return nil
}

func StopAndRemoveContainer(ctx context.Context, containerId string) error {
	defer func() {
		os.RemoveAll(GetContainerDir(containerId))
	}()

	cli := clients.GetDockerClient()
	err := stopContainer(ctx, containerId)
	if err != nil {
		PostContainerStatus(ctx, containerId, pb.Status_STATUS_ERROR)
		return err
	}
	log.Printf("Docker container %s stopped", containerId)

	_, err = cli.ContainerRemove(ctx, containerId, client.ContainerRemoveOptions{})
	if err != nil {
		log.Printf("Failed to remove container %s: %v", containerId, err)
		return err
	}
	log.Printf("Docker container %s removed", containerId)

	PostContainerStatus(ctx, containerId, pb.Status_STATUS_REMOVED)
	return nil
}

func GetContainerLogs(ctx context.Context, containerId string) (*client.ContainerLogsResult, error) {
	cli := clients.GetDockerClient()

	res, err := cli.ContainerLogs(ctx, containerId, client.ContainerLogsOptions{
		ShowStdout: true,
		ShowStderr: true,
	})

	if err != nil {
		log.Printf("Failed to get container %s logs: %v", containerId, err)
		return nil, err
	}

	return &res, nil
}

// TODO: Change stream to fetch on demand
func GetContainerResources(ctx context.Context, containerId string) error {
	cli := clients.GetDockerClient()

	stats, err := cli.ContainerStats(ctx, containerId, client.ContainerStatsOptions{
		Stream: true,
	})

	if err != nil {
		log.Printf("Failed to get container %s stats: %v", containerId, err)
		return err
	}

	decoder := json.NewDecoder(stats.Body)
	log.Printf("Streaming container %s stats", containerId)

	for {
		select {
		case <-ctx.Done():
			log.Printf("Cancellation signal received for stats, exiting")
			return ctx.Err()
		default:
		}

		var containerStats container.StatsResponse
		if err := decoder.Decode(&containerStats); err != nil {
			if err == io.EOF {
				break
			} else if err != nil {
				log.Printf("Error receiving container %s stats: %v", containerId, err)
				break
			}
		}

		PostContainerUtilization(ctx, containerId, Utilization{
			CpuUtil:  int64(containerStats.MemoryStats.Usage),
			CpuTotal: int64(containerStats.MemoryStats.Limit),
			GpuUtil:  0,
			GpuTotal: 0,
		})
	}

	defer stats.Body.Close()

	return nil
}

func InspectContainer(ctx context.Context, containerId string, checkpointId string) (*ContainerConfig, error) {
	cli := clients.GetDockerClient()

	res, err := cli.ContainerInspect(ctx, containerId, client.ContainerInspectOptions{
		Size: false,
	})

	if err != nil {
		return nil, err
	}

	return &ContainerConfig{
		CheckpointId: checkpointId,
		ResourceAllocation: ResourceAllocation{
			CpuAlloc: res.Container.HostConfig.Resources.Memory,
			GpuAlloc: 0,
		},
	}, nil
}

func ContainerCleanup(ctx context.Context) {
	cli := clients.GetDockerClient()

	containers, err := cli.ContainerList(ctx, client.ContainerListOptions{
		All: true,
	})
	if err != nil {
		log.Printf("Error listing containers")
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), time.Second*5)
	defer cancel()

	for _, container := range containers.Items {
		StopAndRemoveContainer(ctx, container.ID)
	}
}
