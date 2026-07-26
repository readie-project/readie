package registry_test

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/logging"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/registry"
	"github.com/illinoisdata/checkpoint-restore-for-serverless/worker/internal/testutil/fakeregistry"
	pb "github.com/illinoisdata/checkpoint-restore-for-serverless/worker/proto"
)

// dialFake serves a fakeregistry.Server over bufconn and returns a client for it.
func dialFake(t *testing.T, fake *fakeregistry.Server) pb.RegistryServiceClient {
	t.Helper()

	lis := bufconn.Listen(1024 * 1024)
	srv := grpc.NewServer()
	pb.RegisterRegistryServiceServer(srv, fake)

	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)

	// The "passthrough:///" scheme is required: grpc.NewClient defaults to the
	// DNS resolver and would try to resolve "bufnet" as a hostname.
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
			return lis.DialContext(ctx)
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	require.NoError(t, err)
	t.Cleanup(func() { _ = conn.Close() })

	return pb.NewRegistryServiceClient(conn)
}

func newReporter(t *testing.T, fake *fakeregistry.Server) *registry.GRPCReporter {
	t.Helper()
	return registry.NewGRPCReporter(dialFake(t, fake), "worker-1", "worker:50052", time.Second, logging.Discard())
}

// The router indexes workers by ID and dials the advertised URI verbatim, so
// both must appear on the wire exactly as configured.
func TestWorkerStatus_SendsTheRouterContractFields(t *testing.T) {
	fake := fakeregistry.NewServer()
	require.NoError(t, newReporter(t, fake).WorkerStatus(context.Background(), pb.Status_STATUS_READY))

	received := fake.WorkerStatuses()
	require.Len(t, received, 1)
	assert.Equal(t, "worker-1", received[0].GetWorkerId())
	assert.Equal(t, "worker:50052", received[0].GetWorkerUri())
	assert.Equal(t, pb.Status_STATUS_READY, received[0].GetStatus())
}

// The identifiers travel as an explicit argument; the previous implementation
// pulled them from a context value and panicked when one was absent.
func TestExecutorStatus_CarriesTheExecutionIdentifiers(t *testing.T) {
	fake := fakeregistry.NewServer()
	ref := registry.ExecutionRef{RequestID: "req-1", SessionID: "sess-1"}

	require.NoError(t, newReporter(t, fake).ExecutorStatus(context.Background(), ref, "cid", pb.Status_STATUS_BUSY))

	received := fake.ExecutorStatuses()
	require.Len(t, received, 1)
	assert.Equal(t, "cid", received[0].GetContainerId())
	assert.Equal(t, "worker-1", received[0].GetWorkerId())
	assert.Equal(t, "req-1", received[0].GetRequestId())
	assert.Equal(t, "sess-1", received[0].GetSessionId())
	assert.Equal(t, pb.Status_STATUS_BUSY, received[0].GetStatus())
}

func TestExecutorUtilization_SendsAllFields(t *testing.T) {
	fake := fakeregistry.NewServer()
	u := registry.Utilization{CPUUtil: 42, CPUTotal: 400, GPUUtil: 1, GPUTotal: 2}

	require.NoError(t, newReporter(t, fake).ExecutorUtilization(context.Background(), "cid", u))

	received := fake.ExecutorUtilizations()
	require.Len(t, received, 1)
	assert.Equal(t, int64(42), received[0].GetCpuUtil())
	assert.Equal(t, int64(400), received[0].GetCpuTotal())
	assert.Equal(t, int64(1), received[0].GetGpuUtil())
	assert.Equal(t, int64(2), received[0].GetGpuTotal())
}

// "The router declined the update" and "the router is unreachable" are
// different failures; the previous bool return conflated them.
func TestReporter_DistinguishesDeclinedFromUnreachable(t *testing.T) {
	t.Run("declined", func(t *testing.T) {
		fake := fakeregistry.NewServer()
		fake.Updated = false

		err := newReporter(t, fake).WorkerStatus(context.Background(), pb.Status_STATUS_READY)
		require.Error(t, err)
		assert.ErrorIs(t, err, registry.ErrNotUpdated)
		assert.NotErrorIs(t, err, registry.ErrRouterUnavailable)
	})

	t.Run("unreachable", func(t *testing.T) {
		fake := fakeregistry.NewServer()
		fake.Err = status.Error(codes.Unavailable, "connection refused")

		err := newReporter(t, fake).WorkerStatus(context.Background(), pb.Status_STATUS_READY)
		require.Error(t, err)
		assert.ErrorIs(t, err, registry.ErrRouterUnavailable)
		assert.NotErrorIs(t, err, registry.ErrNotUpdated)
	})

	t.Run("other rpc failure", func(t *testing.T) {
		fake := fakeregistry.NewServer()
		fake.Err = status.Error(codes.Internal, "boom")

		err := newReporter(t, fake).WorkerStatus(context.Background(), pb.Status_STATUS_READY)
		require.Error(t, err)
		assert.NotErrorIs(t, err, registry.ErrRouterUnavailable)
		assert.NotErrorIs(t, err, registry.ErrNotUpdated)
	})
}

func TestNopReporter_SatisfiesTheInterfaceWithoutError(t *testing.T) {
	var r registry.Reporter = registry.NewNopReporter()
	ctx := context.Background()

	assert.NoError(t, r.WorkerStatus(ctx, pb.Status_STATUS_READY))
	assert.NoError(t, r.ExecutorStatus(ctx, registry.ExecutionRef{}, "cid", pb.Status_STATUS_BUSY))
	assert.NoError(t, r.WorkerUtilization(ctx, registry.Utilization{}))
	assert.NoError(t, r.ExecutorUtilization(ctx, "cid", registry.Utilization{}))
}
