package integration

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/reflection/grpc_reflection_v1"
	"google.golang.org/grpc/status"

	"github.com/illinoisdata/readie/worker/internal/config"
	"github.com/illinoisdata/readie/worker/internal/registry"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakeexecutor"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakeregistry"
	"github.com/illinoisdata/readie/worker/internal/testutil/fakesandbox"
	pb "github.com/illinoisdata/readie/worker/proto"
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
	// Release now runs in the background after the RPC returns, so poll for it.
	require.Eventually(t, func() bool {
		state, ok := h.Runtime.Get(containerID)
		return ok && state.Paused
	}, 5*time.Second, 20*time.Millisecond, "the container must eventually be paused for reuse")

	require.NoError(t, h.Shutdown())

	state, ok := h.Runtime.Get(containerID)
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

// Release now finishes in the background rather than before the RPC returns
// (see Runner.WaitPendingReleases), so a container's teardown can still be
// running when shutdown begins. Shutdown must wait for it rather than race
// CleanupOrphans against it: a race here would either surface as a spurious
// "could not reclaim containers" error, or as a container the shutdown left
// behind because CleanupOrphans checked before the async release landed.
func TestShutdown_WaitsForABackgroundReleaseBeforeReclaimingOrphans(t *testing.T) {
	h := newHarness(t, withFakes(func(d *fakesandbox.Sandbox, _ *fakeregistry.Server) {
		d.StopDelay = 300 * time.Millisecond
	}))

	responses, err := execute(t, h, "", []byte("body"))
	require.NoError(t, err)
	containerID := responses[0].GetContainerId()

	// The release this execution triggered is very likely still mid-teardown
	// (Stop is asleep) at the instant shutdown starts.
	require.NoError(t, h.Shutdown())

	state, ok := h.Runtime.Get(containerID)
	require.True(t, ok)
	assert.True(t, state.Removed, "the container must not be left running after shutdown")
	assert.NoDirExists(t, h.Layout.ContainerDir(containerID))

	// The real assertion: CleanupOrphans must not have found this container
	// still around to destroy a second time. Both Destroy and CleanupOrphans
	// tolerate a redundant destroy without erroring, so an end state of
	// "removed, no directory" is reachable even if the wait did nothing and
	// the two raced - only the call count tells them apart.
	assert.Equal(t, 1, h.Runtime.StopCallCount(containerID),
		"the background release must finish before CleanupOrphans looks for orphans, not race it")
}

// A worker image built with no artifacts baked in - what `docker compose build`
// produces before any checkpoint has been captured.
//
// It starts. A worker that exits before it logs is far harder to diagnose than
// one that is running and says why it is useless, and a crash-looping container
// tells an operator nothing about which of a dozen causes applies.

func TestStartup_SucceedsWithNoRootfs(t *testing.T) {
	h := newHarness(t, withoutArtifacts())

	require.NotNil(t, h.App)
	assert.NotNil(t, h.App.Addr(), "the gRPC server is listening")
}

// ERROR rather than READY: the router's is_selectable requires READY, so this
// keeps the worker visible and probed while never placing work on it.
// Registering READY would route executions here that can only fail; not
// registering at all would show an operator "no workers" with no hint that one
// is running.
func TestStartup_WithoutARootfsRegistersAsErrorNotReady(t *testing.T) {
	h := newHarness(t, withoutArtifacts())

	require.True(t, h.Router.WaitFor(func(s *fakeregistry.Server) bool {
		return len(s.WorkerStatuses()) > 0
	}, 5*time.Second))

	registered := h.Router.WorkerStatuses()[0]
	assert.Equal(t, pb.Status_STATUS_ERROR, registered.GetStatus())
	assert.Equal(t, "worker-1", registered.GetWorkerId(),
		"it still identifies itself, so an operator can see which node is unusable")
	assert.Equal(t, "worker:50052", registered.GetWorkerUri())
}

// Health tracks the process, not the artifacts. NOT_SERVING would have the
// router's prober evict the worker after three strikes, hiding the very state
// this change exists to make visible.
func TestStartup_WithoutARootfsStillServesHealth(t *testing.T) {
	h := newHarness(t, withoutArtifacts())

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	res, err := h.Health.Check(ctx, &healthpb.HealthCheckRequest{})

	require.NoError(t, err)
	assert.Equal(t, healthpb.HealthCheckResponse_SERVING, res.GetStatus())
}

func TestExecution_WithoutARootfsFailsNamingTheMissingArtifact(t *testing.T) {
	h := newHarness(t, withoutArtifacts())

	_, err := execute(t, h, "", []byte("body"))

	require.Error(t, err)
	// FailedPrecondition, not ResourceExhausted: the worker is not out of
	// capacity, and reporting that would send an operator looking at memory.
	assert.Equal(t, codes.FailedPrecondition, status.Code(err))
	assert.Contains(t, err.Error(), "no root filesystem",
		"the failure has to name the artifact, not surface from inside runsc")
	assert.Contains(t, err.Error(), "make generation",
		"and say what to do about it")
}
