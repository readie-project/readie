// Package fakeregistry provides test doubles for the router's RegistryService.
//
// Recorder implements registry.Reporter for unit tests that only care which
// reports were emitted. Server implements the generated gRPC service so
// integration tests can assert what the real router would have received.
package fakeregistry

import (
	"context"
	"sync"
	"time"

	"github.com/illinoisdata/readie/worker/internal/registry"
	pb "github.com/illinoisdata/readie/worker/proto"
)

// Call is one recorded report.
type Call struct {
	Method      string // "WorkerStatus", "ExecutorStatus", "WorkerUtilization", "ExecutorUtilization"
	Status      pb.Status
	ContainerID string
	Ref         registry.ExecutionRef
	Utilization registry.Utilization
}

// Recorder is a registry.Reporter that records calls and can inject failures.
// It is safe for concurrent use.
type Recorder struct {
	mu     sync.Mutex
	calls  []Call
	errs   map[string]error
	notify chan struct{}
}

var _ registry.Reporter = (*Recorder)(nil)

// NewRecorder returns an empty Recorder.
func NewRecorder() *Recorder {
	return &Recorder{
		errs:   make(map[string]error),
		notify: make(chan struct{}, 1),
	}
}

// FailOn makes the named method return err on every subsequent call.
func (r *Recorder) FailOn(method string, err error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.errs[method] = err
}

// Calls returns a copy of everything recorded so far.
func (r *Recorder) Calls() []Call {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Call(nil), r.calls...)
}

// StatusesFor returns the statuses reported for a container, in order.
func (r *Recorder) StatusesFor(containerID string) []pb.Status {
	r.mu.Lock()
	defer r.mu.Unlock()

	var statuses []pb.Status
	for _, c := range r.calls {
		if c.Method == "ExecutorStatus" && c.ContainerID == containerID {
			statuses = append(statuses, c.Status)
		}
	}
	return statuses
}

// WorkerStatuses returns the worker-level statuses reported, in order.
func (r *Recorder) WorkerStatuses() []pb.Status {
	r.mu.Lock()
	defer r.mu.Unlock()

	var statuses []pb.Status
	for _, c := range r.calls {
		if c.Method == "WorkerStatus" {
			statuses = append(statuses, c.Status)
		}
	}
	return statuses
}

// WaitFor blocks until pred is satisfied by the recorded calls, or timeout
// elapses. It reports whether pred was satisfied.
func (r *Recorder) WaitFor(pred func([]Call) bool, timeout time.Duration) bool {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	for {
		if pred(r.Calls()) {
			return true
		}
		select {
		case <-r.notify:
		case <-deadline.C:
			return pred(r.Calls())
		}
	}
}

func (r *Recorder) record(c Call) error {
	r.mu.Lock()
	err := r.errs[c.Method]
	r.calls = append(r.calls, c)
	r.mu.Unlock()

	select {
	case r.notify <- struct{}{}:
	default:
	}
	return err
}

// WorkerStatus records a worker status report.
func (r *Recorder) WorkerStatus(_ context.Context, status pb.Status) error {
	return r.record(Call{Method: "WorkerStatus", Status: status})
}

// ExecutorStatus records an executor status report.
func (r *Recorder) ExecutorStatus(_ context.Context, ref registry.ExecutionRef, containerID string, status pb.Status) error {
	return r.record(Call{Method: "ExecutorStatus", Ref: ref, ContainerID: containerID, Status: status})
}

// WorkerUtilization records a worker utilization report.
func (r *Recorder) WorkerUtilization(_ context.Context, u registry.Utilization) error {
	return r.record(Call{Method: "WorkerUtilization", Utilization: u})
}

// ExecutorUtilization records an executor utilization report.
func (r *Recorder) ExecutorUtilization(_ context.Context, containerID string, u registry.Utilization) error {
	return r.record(Call{Method: "ExecutorUtilization", ContainerID: containerID, Utilization: u})
}

// Server implements the generated RegistryService so a real gRPC client can be
// pointed at it. It records the protobuf messages verbatim, which is what makes
// it useful for asserting the exact wire contract with the router.
type Server struct {
	pb.UnimplementedRegistryServiceServer

	mu           sync.Mutex
	workerStatus []*pb.WorkerStatus
	execStatus   []*pb.ExecutorStatus
	workerUtil   []*pb.WorkerUtilization
	execUtil     []*pb.ExecutorUtilization

	// Updated is what every response reports. Set false to exercise the
	// worker's handling of a router that declines an update.
	Updated bool
	// Err, when set, is returned from every RPC.
	Err error

	notify chan struct{}
}

var _ pb.RegistryServiceServer = (*Server)(nil)

// NewServer returns a Server that accepts every update.
func NewServer() *Server {
	return &Server{Updated: true, notify: make(chan struct{}, 1)}
}

func (s *Server) respond() (*pb.RegistryUpdateResponse, error) {
	select {
	case s.notify <- struct{}{}:
	default:
	}
	if s.Err != nil {
		return nil, s.Err
	}
	return &pb.RegistryUpdateResponse{Updated: s.Updated}, nil
}

// PostWorkerStatus records a worker status message.
func (s *Server) PostWorkerStatus(_ context.Context, in *pb.WorkerStatus) (*pb.RegistryUpdateResponse, error) {
	s.mu.Lock()
	s.workerStatus = append(s.workerStatus, in)
	s.mu.Unlock()
	return s.respond()
}

// PostExecutorStatus records an executor status message.
func (s *Server) PostExecutorStatus(_ context.Context, in *pb.ExecutorStatus) (*pb.RegistryUpdateResponse, error) {
	s.mu.Lock()
	s.execStatus = append(s.execStatus, in)
	s.mu.Unlock()
	return s.respond()
}

// PostWorkerUtilization records a worker utilization message.
func (s *Server) PostWorkerUtilization(_ context.Context, in *pb.WorkerUtilization) (*pb.RegistryUpdateResponse, error) {
	s.mu.Lock()
	s.workerUtil = append(s.workerUtil, in)
	s.mu.Unlock()
	return s.respond()
}

// PostExecutorUtilization records an executor utilization message.
func (s *Server) PostExecutorUtilization(_ context.Context, in *pb.ExecutorUtilization) (*pb.RegistryUpdateResponse, error) {
	s.mu.Lock()
	s.execUtil = append(s.execUtil, in)
	s.mu.Unlock()
	return s.respond()
}

// WorkerStatuses returns the worker status messages received, in order.
func (s *Server) WorkerStatuses() []*pb.WorkerStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.WorkerStatus(nil), s.workerStatus...)
}

// ExecutorStatuses returns the executor status messages received, in order.
func (s *Server) ExecutorStatuses() []*pb.ExecutorStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.ExecutorStatus(nil), s.execStatus...)
}

// WorkerUtilizations returns the worker utilization messages received, in order.
func (s *Server) WorkerUtilizations() []*pb.WorkerUtilization {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.WorkerUtilization(nil), s.workerUtil...)
}

// ExecutorUtilizations returns the executor utilization messages received.
func (s *Server) ExecutorUtilizations() []*pb.ExecutorUtilization {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*pb.ExecutorUtilization(nil), s.execUtil...)
}

// WaitFor blocks until pred holds or timeout elapses, reporting whether it held.
func (s *Server) WaitFor(pred func(*Server) bool, timeout time.Duration) bool {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	for {
		if pred(s) {
			return true
		}
		select {
		case <-s.notify:
		case <-deadline.C:
			return pred(s)
		}
	}
}
