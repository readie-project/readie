package registry

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/readie/worker/internal/logging"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// GRPCReporter posts state to the router's RegistryService.
type GRPCReporter struct {
	client    pb.RegistryServiceClient
	workerID  string
	workerURI string
	capacity  Capacity
	timeout   time.Duration
	log       *slog.Logger
}

var _ Reporter = (*GRPCReporter)(nil)

// NewGRPCReporter builds a reporter.
//
// workerID and workerURI are the identity the router indexes this worker by;
// it dials workerURI verbatim when routing an execution here.
func NewGRPCReporter(
	client pb.RegistryServiceClient,
	workerID, workerURI string,
	capacity Capacity,
	timeout time.Duration,
	log *slog.Logger,
) *GRPCReporter {
	if log == nil {
		log = slog.Default()
	}
	if timeout <= 0 {
		timeout = 5 * time.Second
	}
	return &GRPCReporter{
		client:    client,
		workerID:  workerID,
		workerURI: workerURI,
		capacity:  capacity,
		timeout:   timeout,
		log:       log.With(logging.KeyWorkerID, workerID),
	}
}

// WorkerStatus reports this worker's lifecycle state.
func (r *GRPCReporter) WorkerStatus(ctx context.Context, workerStatus pb.Status) error {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	res, err := r.client.PostWorkerStatus(ctx, &pb.WorkerStatus{
		WorkerId:     r.workerID,
		WorkerUri:    r.workerURI,
		Status:       workerStatus,
		MemTotal:     r.capacity.MemTotal,
		MaxExecutors: r.capacity.MaxExecutors,
		Flavor:       r.capacity.Flavor,
		GpuMemTotal:  r.capacity.GPUTotal,
	})
	return r.check("worker status", err, res)
}

// ExecutorStatus reports a container's lifecycle state against the request
// that owns it.
func (r *GRPCReporter) ExecutorStatus(
	ctx context.Context,
	ref ExecutionRef,
	containerID string,
	executorStatus pb.Status,
) error {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	res, err := r.client.PostExecutorStatus(ctx, &pb.ExecutorStatus{
		ContainerId: containerID,
		WorkerId:    r.workerID,
		RequestId:   ref.RequestID,
		SessionId:   ref.SessionID,
		Status:      executorStatus,
	})
	return r.check("executor status", err, res)
}

// WorkerUtilization reports this worker's resource usage.
func (r *GRPCReporter) WorkerUtilization(ctx context.Context, u Utilization) error {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	res, err := r.client.PostWorkerUtilization(ctx, &pb.WorkerUtilization{
		WorkerId:      r.workerID,
		CpuUtil:       u.CPUUtil,
		CpuTotal:      u.CPUTotal,
		GpuUtil:       u.GPUUtil,
		GpuTotal:      u.GPUTotal,
		MemUsed:       u.MemUsed,
		MemTotal:      u.MemTotal,
		ExecutorCount: u.ExecutorCount,
		GpuMemUsed:    u.GPUMemUsed,
		GpuMemTotal:   u.GPUMemTotal,
	})
	return r.check("worker utilization", err, res)
}

// ExecutorUtilization reports a container's resource usage.
func (r *GRPCReporter) ExecutorUtilization(ctx context.Context, containerID string, u Utilization) error {
	ctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	res, err := r.client.PostExecutorUtilization(ctx, &pb.ExecutorUtilization{
		ContainerId: containerID,
		WorkerId:    r.workerID,
		CpuUtil:     u.CPUUtil,
		CpuTotal:    u.CPUTotal,
		GpuUtil:     u.GPUUtil,
		GpuTotal:    u.GPUTotal,
	})
	return r.check("executor utilization", err, res)
}

// check converts a transport error or a declined update into an error a caller
// can classify. The previous implementation collapsed both into a bool, which
// made "the router is down" indistinguishable from "the router said no".
func (r *GRPCReporter) check(op string, err error, res *pb.RegistryUpdateResponse) error {
	if err != nil {
		if isUnavailable(err) {
			return fmt.Errorf("post %s: %w: %w", op, ErrRouterUnavailable, err)
		}
		return fmt.Errorf("post %s: %w", op, err)
	}
	if res == nil || !res.GetUpdated() {
		return fmt.Errorf("post %s: %w", op, ErrNotUpdated)
	}
	return nil
}

func isUnavailable(err error) bool {
	switch status.Code(err) {
	case codes.Unavailable, codes.DeadlineExceeded:
		return true
	default:
		return false
	}
}
