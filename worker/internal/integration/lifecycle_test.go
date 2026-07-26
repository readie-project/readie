package integration

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/reflection/grpc_reflection_v1"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/config"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeexecutor"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakesandbox"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

// docker-compose's healthcheck shells grpcurl against these two services, so
// both must stay registered.
func TestHealthAndReflectionAreServed(t *testing.T) {
	h := newHarness(t)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()

	res, err := h.Health.Check(ctx, &healthpb.HealthCheckRequest{})
	require.NoError(t, err)
	assert.Equal(t, healthpb.HealthCheckResponse_SERVING, res.GetStatus())

	// Reflection is what lets grpcurl work without a proto descriptor.
	reflectClient := grpc_reflection_v1.NewServerReflectionClient(
		dialBufconnFor(t, h))
	stream, err := reflectClient.ServerReflectionInfo(ctx)
	require.NoError(t, err)

	require.NoError(t, stream.Send(&grpc_reflection_v1.ServerReflectionRequest{
		MessageRequest: &grpc_reflection_v1.ServerReflectionRequest_ListServices{},
	}))
	reflectRes, err := stream.Recv()
	require.NoError(t, err)

	var services []string
	for _, s := range reflectRes.GetListServicesResponse().GetService() {
		services = append(services, s.GetName())
	}
	assert.Contains(t, services, "ExecutionService")
	assert.Contains(t, services, "grpc.health.v1.Health")
}

// The router indexes this worker by ID and later dials the URI it registered,
// so both must arrive exactly as configured.
func TestStartup_RegistersWithTheRouter(t *testing.T) {
	h := newHarness(t)

	require.True(t, h.Router.WaitFor(func(s *fakeregistry.Server) bool {
		return len(s.WorkerStatuses()) > 0
	}, 5*time.Second))

	registered := h.Router.WorkerStatuses()[0]
	assert.Equal(t, "worker-1", registered.GetWorkerId())
	assert.Equal(t, "worker:50052", registered.GetWorkerUri())
	assert.Equal(t, pb.Status_STATUS_READY, registered.GetStatus())
}

// A process killed without warning leaves containers and directories behind.
// Startup is the only opportunity to reclaim them; the previous implementation
// swept only on shutdown, so a crash leaked them permanently.
func TestStartup_ReclaimsOrphansBeforeServing(t *testing.T) {
	orphans := []string{
		config.ContainerNamePrefix + "orphan-a",
		config.ContainerNamePrefix + "orphan-b",
	}

	h := newHarness(t, withFakes(func(d *fakesandbox.Sandbox, _ *fakeregistry.Server) {
		for _, id := range orphans {
			d.Seed(id)
		}
	}))

	// newHarness returns only once health reports SERVING, so reclamation has
	// already happened by the time this runs.
	for _, id := range orphans {
		state, ok := h.Runtime.Get(id)
		require.True(t, ok)
		assert.True(t, state.Removed, "%s should have been reclaimed at startup", id)
	}
	assert.Empty(t, h.Runtime.LiveIDs())
}

func TestStartup_LeavesForeignContainersAlone(t *testing.T) {
	h := newHarness(t, withFakes(func(d *fakesandbox.Sandbox, _ *fakeregistry.Server) {
		d.Seed("some-other-service")
	}))

	state, ok := h.Runtime.Get("some-other-service")
	require.True(t, ok)
	assert.False(t, state.Removed, "containers this worker does not own must survive")
}

// Health must not report SERVING until the worker can actually serve, so an
// orchestrator never routes to a half-initialised process.
func TestStartup_FailsWhenTheRouterRejectsRegistration(t *testing.T) {
	err := newHarnessExpectingFailure(t, func(_ *fakesandbox.Sandbox, r *fakeregistry.Server) {
		r.Updated = false // the router declines every update
	})

	require.Error(t, err, "startup must fail when the worker cannot register")
	assert.ErrorIs(t, err, registry.ErrNotUpdated)
}

func TestShutdown_DeregistersAndReclaimsContainers(t *testing.T) {
	h := newHarness(t)

	responses, err := execute(t, h, "", []byte("body"))
	require.NoError(t, err)
	containerID := responses[0].GetContainerId()

	// The container is warm and paused, so shutdown has something to reclaim.
	state, ok := h.Runtime.Get(containerID)
	require.True(t, ok)
	require.True(t, state.Paused)

	require.NoError(t, h.Shutdown())

	state, ok = h.Runtime.Get(containerID)
	require.True(t, ok)
	assert.True(t, state.Removed, "warm containers must not outlive the worker")
	assert.NoDirExists(t, h.Layout.ContainerDir(containerID))

	// Deregistration happens before the server drains, so the router stops
	// routing here while in-flight work finishes.
	statuses := h.Router.WorkerStatuses()
	require.NotEmpty(t, statuses)
	assert.Equal(t, pb.Status_STATUS_REMOVED, statuses[len(statuses)-1].GetStatus())

	assert.True(t, h.Runtime.Closed(), "the container runtime client must be closed")
}

// In-flight streams must be allowed to finish, because each one releases its
// own container on the way out.
func TestShutdown_DrainsAnInFlightExecution(t *testing.T) {
	h := newHarness(t, withExecutor(fakeexecutor.Options{
		Mode:          fakeexecutor.ModeSlowResponse,
		ResponseDelay: 500 * time.Millisecond,
		Reply:         []byte("finished-during-drain"),
	}))

	type outcome struct {
		responses []*pb.WorkerExecutionResponse
		err       error
	}
	done := make(chan outcome, 1)

	go func() {
		responses, err := execute(t, h, "", []byte("body"))
		done <- outcome{responses, err}
	}()

	// Let the execution get underway, then ask the worker to stop.
	require.Eventually(t, func() bool { return len(h.Runtime.IDs()) > 0 }, 10*time.Second, 20*time.Millisecond)
	shutdownErr := make(chan error, 1)
	go func() { shutdownErr <- h.Shutdown() }()

	select {
	case got := <-done:
		require.NoError(t, got.err, "an in-flight execution must be drained, not severed")
		assert.Equal(t, "finished-during-drain", string(payloadOf(got.responses)))
	case <-time.After(30 * time.Second):
		t.Fatal("the in-flight execution never completed")
	}

	select {
	case err := <-shutdownErr:
		assert.NoError(t, err)
	case <-time.After(30 * time.Second):
		t.Fatal("shutdown never completed")
	}
}

func TestShutdown_IsIdempotent(t *testing.T) {
	h := newHarness(t)

	require.NoError(t, h.Shutdown())
	require.NoError(t, h.App.Shutdown(context.Background()))
}

func TestShutdown_LeavesNoContainerDirectoriesBehind(t *testing.T) {
	h := newHarness(t)

	for range 3 {
		_, err := execute(t, h, "", []byte("body"))
		require.NoError(t, err)
	}
	require.NoError(t, h.Shutdown())

	entries, err := os.ReadDir(h.WorkerDir)
	require.NoError(t, err)
	assert.Empty(t, entries, "every per-container directory must be reclaimed")
}
