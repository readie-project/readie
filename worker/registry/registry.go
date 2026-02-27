package registry

import (
	"context"
	"log"
	"worker/config"
	pb "worker/proto"
	"worker/rpc"
)

func PostWorkerStatus(ctx context.Context, status pb.Status) bool {
	registry := *rpc.GetRegistryClient()

	res, err := registry.PostWorkerStatus(ctx, &pb.WorkerStatus{
		WorkerId:  config.WorkerId,
		WorkerUri: config.WorkerNodeUri,
		Status:    status,
	})
	if err != nil {
		log.Printf("Could not update worker %s status to main node: %v", config.WorkerId, err)
	}
	return (err != nil) && res.Updated
}

func PostContainerStatus(ctx context.Context, containerId string, status pb.Status) bool {
	registry := *rpc.GetRegistryClient()

	res, err := registry.PostExecutorStatus(ctx, &pb.ExecutorStatus{
		WorkerId:    config.WorkerId,
		ContainerId: containerId,
		Status:      status,
	})
	if err != nil {
		log.Printf("Could not update container %s~%s status to main node: %v", config.WorkerId, containerId, err)
	}
	return (err != nil) && res.Updated
}

type Utilization struct {
	CpuUtil  int64
	CpuTotal int64
	GpuUtil  int64
	GpuTotal int64
}

func PostWorkerUtilization(ctx context.Context, utilization Utilization) bool {
	registry := *rpc.GetRegistryClient()

	res, err := registry.PostWorkerUtilization(ctx, &pb.WorkerUtilization{
		WorkerId: config.WorkerId,
		CpuUtil:  utilization.CpuUtil,
		CpuTotal: utilization.CpuTotal,
		GpuUtil:  utilization.GpuUtil,
		GpuTotal: utilization.GpuTotal,
	})
	if err != nil {
		log.Printf("Could not update worker %s utilization to main node: %v", config.WorkerId, err)
	}
	return (err != nil) && res.Updated
}

func PostContainerUtilization(ctx context.Context, containerId string, utilization Utilization) bool {
	registry := *rpc.GetRegistryClient()

	res, err := registry.PostExecutorUtilization(ctx, &pb.ExecutorUtilization{
		WorkerId:    config.WorkerId,
		ContainerId: containerId,
		CpuUtil:     utilization.CpuUtil,
		CpuTotal:    utilization.CpuTotal,
		GpuUtil:     utilization.GpuUtil,
		GpuTotal:    utilization.GpuTotal,
	})
	if err != nil {
		log.Printf("Could not update container %s~%s utilization to main node: %v", config.WorkerId, containerId, err)
	}
	return (err != nil) && res.Updated
}
