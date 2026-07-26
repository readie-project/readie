package docker

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/containerd/errdefs"
	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// stubAPI is a minimal mobyAPI recording what the adapter asked for. It exists
// to test translation, not daemon behaviour.
type stubAPI struct {
	mobyAPI // embedded so unexercised methods panic rather than needing stubs

	createOpts client.ContainerCreateOptions
	startOpts  client.ContainerStartOptions
	startID    string
	removeOpts client.ContainerRemoveOptions
	listOpts   client.ContainerListOptions
	inspectRes client.ContainerInspectResult
	listRes    client.ContainerListResult
	logsRes    client.ContainerLogsResult
	statsRes   client.ContainerStatsResult
	statsOpts  client.ContainerStatsOptions
	returnErr  error
}

func (s *stubAPI) ContainerCreate(_ context.Context, opts client.ContainerCreateOptions) (client.ContainerCreateResult, error) {
	s.createOpts = opts
	if s.returnErr != nil {
		return client.ContainerCreateResult{}, s.returnErr
	}
	return client.ContainerCreateResult{ID: "container-id", Warnings: []string{"a warning"}}, nil
}

func (s *stubAPI) ContainerStart(_ context.Context, id string, opts client.ContainerStartOptions) (client.ContainerStartResult, error) {
	s.startID, s.startOpts = id, opts
	return client.ContainerStartResult{}, s.returnErr
}

func (s *stubAPI) ContainerRemove(_ context.Context, _ string, opts client.ContainerRemoveOptions) (client.ContainerRemoveResult, error) {
	s.removeOpts = opts
	return client.ContainerRemoveResult{}, s.returnErr
}

func (s *stubAPI) ContainerInspect(_ context.Context, _ string, _ client.ContainerInspectOptions) (client.ContainerInspectResult, error) {
	return s.inspectRes, s.returnErr
}

func (s *stubAPI) ContainerList(_ context.Context, opts client.ContainerListOptions) (client.ContainerListResult, error) {
	s.listOpts = opts
	return s.listRes, s.returnErr
}

func (s *stubAPI) ContainerLogs(_ context.Context, _ string, _ client.ContainerLogsOptions) (client.ContainerLogsResult, error) {
	return s.logsRes, s.returnErr
}

func (s *stubAPI) ContainerStats(_ context.Context, _ string, opts client.ContainerStatsOptions) (client.ContainerStatsResult, error) {
	s.statsOpts = opts
	return s.statsRes, s.returnErr
}

func newTestAdapter(api *stubAPI) *MobyAdapter {
	return NewMobyAdapter(api, slog.New(slog.DiscardHandler))
}

// The create specification is a contract with the Python executor: it finds its
// socket through EXECUTOR_DIR and its imports through PYTHONPATH, both of which
// only resolve because of these bind mounts.
func TestCreate_TranslatesTheFullSpec(t *testing.T) {
	api := &stubAPI{}

	id, err := newTestAdapter(api).Create(context.Background(), CreateSpec{
		Name:        "exec_container-abc",
		Image:       "test-agent",
		Env:         []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/tmp/site_packages"},
		NetworkMode: "none",
		Binds:       []string{"/shared/exec_container-abc:/tmp:rw", "/site-packages:/tmp/site_packages:ro"},
		MemoryBytes: 512 << 20,
		CPUQuota:    50000,
		PidsLimit:   100,
		Runtime:     "runsc",
	})
	require.NoError(t, err)
	assert.Equal(t, "container-id", id)

	assert.Equal(t, "exec_container-abc", api.createOpts.Name)
	require.NotNil(t, api.createOpts.Config)
	assert.Equal(t, "test-agent", api.createOpts.Config.Image)
	assert.Equal(t, []string{"EXECUTOR_DIR=/tmp", "PYTHONPATH=/tmp/site_packages"}, api.createOpts.Config.Env)

	host := api.createOpts.HostConfig
	require.NotNil(t, host)
	assert.Equal(t, container.NetworkMode("none"), host.NetworkMode)
	assert.Equal(t, "runsc", host.Runtime)
	assert.Equal(t, []string{"/shared/exec_container-abc:/tmp:rw", "/site-packages:/tmp/site_packages:ro"}, host.Binds)
	assert.Equal(t, int64(512<<20), host.Memory)
	assert.Equal(t, int64(50000), host.CPUQuota)
	require.NotNil(t, host.PidsLimit)
	assert.Equal(t, int64(100), *host.PidsLimit)
}

func TestStart_OmitsCheckpointFieldsForAColdStart(t *testing.T) {
	api := &stubAPI{}
	require.NoError(t, newTestAdapter(api).Start(context.Background(), "cid", StartSpec{
		CheckpointDir: "/shared/checkpoints",
	}))

	assert.Equal(t, "cid", api.startID)
	assert.Empty(t, api.startOpts.CheckpointID)
	assert.Empty(t, api.startOpts.CheckpointDir,
		"checkpoint dir must not be sent without a checkpoint id")
}

func TestStart_PassesCheckpointFieldsForARestore(t *testing.T) {
	api := &stubAPI{}
	require.NoError(t, newTestAdapter(api).Start(context.Background(), "cid", StartSpec{
		CheckpointID:  "checkpoint_1",
		CheckpointDir: "/shared/checkpoints",
	}))

	assert.Equal(t, "checkpoint_1", api.startOpts.CheckpointID)
	assert.Equal(t, "/shared/checkpoints", api.startOpts.CheckpointDir)
}

func TestInspect_ToleratesNilSubStructs(t *testing.T) {
	api := &stubAPI{inspectRes: client.ContainerInspectResult{
		Container: container.InspectResponse{ID: "cid", Name: "/exec_container-abc"},
	}}

	info, err := newTestAdapter(api).Inspect(context.Background(), "cid")
	require.NoError(t, err)

	assert.Equal(t, "cid", info.ID)
	assert.Equal(t, "exec_container-abc", info.Name, "the daemon's leading slash must be stripped")
	assert.Zero(t, info.MemoryBytes)
	assert.False(t, info.Running)
}

func TestList_AnchorsTheNameFilterAndStripsSlashes(t *testing.T) {
	api := &stubAPI{listRes: client.ContainerListResult{
		Items: []container.Summary{{
			ID:    "cid",
			Names: []string{"/exec_container-abc"},
			State: container.StateRunning,
		}},
	}}

	summaries, err := newTestAdapter(api).List(context.Background(), "exec_container-")
	require.NoError(t, err)

	assert.True(t, api.listOpts.All, "stopped orphans must be listed too")
	assert.Equal(t, map[string]map[string]bool{"name": {"^/exec_container-": true}}, map[string]map[string]bool(api.listOpts.Filters))

	require.Len(t, summaries, 1)
	assert.Equal(t, []string{"exec_container-abc"}, summaries[0].Names)
	assert.Equal(t, "running", summaries[0].State)
}

func TestStats_RequestsAPriorSampleOnlyForOneShotReads(t *testing.T) {
	api := &stubAPI{statsRes: client.ContainerStatsResult{Body: io.NopCloser(bytes.NewReader(nil))}}
	adapter := newTestAdapter(api)

	stream, err := adapter.Stats(context.Background(), "cid", false)
	require.NoError(t, err)
	require.NoError(t, stream.Close())
	assert.True(t, api.statsOpts.IncludePreviousSample,
		"a one-shot sample carries no predecessor, so CPU usage is underivable without one")

	api.statsRes = client.ContainerStatsResult{Body: io.NopCloser(bytes.NewReader(nil))}
	stream, err = adapter.Stats(context.Background(), "cid", true)
	require.NoError(t, err)
	require.NoError(t, stream.Close())
	assert.True(t, api.statsOpts.Stream)
	assert.False(t, api.statsOpts.IncludePreviousSample)
}

func TestStatsStream_DecodesFramesThenReportsEOF(t *testing.T) {
	body := `{"read":"2026-01-01T00:00:00Z","cpu_stats":{"cpu_usage":{"total_usage":200},"system_cpu_usage":2000,"online_cpus":4},` +
		`"precpu_stats":{"cpu_usage":{"total_usage":100},"system_cpu_usage":1000},` +
		`"memory_stats":{"usage":1024,"limit":4096}}`

	api := &stubAPI{statsRes: client.ContainerStatsResult{Body: io.NopCloser(bytes.NewReader([]byte(body)))}}
	stream, err := newTestAdapter(api).Stats(context.Background(), "cid", true)
	require.NoError(t, err)
	t.Cleanup(func() { _ = stream.Close() })

	sample, err := stream.Recv()
	require.NoError(t, err)
	assert.Equal(t, int64(1024), sample.MemoryUsage)
	assert.Equal(t, int64(4096), sample.MemoryLimit)
	assert.Equal(t, int64(200), sample.CPUTotal)
	assert.Equal(t, int64(4), sample.OnlineCPUs)

	// 100ns of container time against 1000ns of host time on 4 CPUs.
	assert.InDelta(t, 40.0, sample.CPUPercent(), 0.001)

	_, err = stream.Recv()
	assert.ErrorIs(t, err, io.EOF)
}

func TestStatsStream_CloseIsIdempotent(t *testing.T) {
	api := &stubAPI{statsRes: client.ContainerStatsResult{Body: io.NopCloser(bytes.NewReader(nil))}}
	stream, err := newTestAdapter(api).Stats(context.Background(), "cid", true)
	require.NoError(t, err)

	require.NoError(t, stream.Close())
	require.NoError(t, stream.Close())
}

func TestCPUPercent_ZeroWithoutAUsablePredecessor(t *testing.T) {
	tests := map[string]Stats{
		"first sample of a stream": {CPUTotal: 100, CPUSystem: 1000, OnlineCPUs: 2},
		"no host time elapsed":     {CPUTotal: 200, PreCPUTotal: 100, CPUSystem: 1000, PreCPUSystem: 1000, OnlineCPUs: 2},
		"counter reset":            {CPUTotal: 50, PreCPUTotal: 100, CPUSystem: 2000, PreCPUSystem: 1000, OnlineCPUs: 2},
	}
	for name, sample := range tests {
		t.Run(name, func(t *testing.T) {
			assert.Zero(t, sample.CPUPercent())
		})
	}
}

func TestCPUPercent_DefaultsToOneCPUWhenTheDaemonOmitsTheCount(t *testing.T) {
	sample := Stats{CPUTotal: 200, PreCPUTotal: 100, CPUSystem: 2000, PreCPUSystem: 1000}
	assert.InDelta(t, 10.0, sample.CPUPercent(), 0.001)
}

// Containers run without a TTY, so the daemon frames output with an 8-byte
// header the router must never see.
func TestLogs_DemultiplexesTheFramedStream(t *testing.T) {
	var framed bytes.Buffer
	writeLogFrame(&framed, 1, "stdout line\n")
	writeLogFrame(&framed, 2, "stderr line\n")

	api := &stubAPI{logsRes: io.NopCloser(bytes.NewReader(framed.Bytes()))}
	logs, err := newTestAdapter(api).Logs(context.Background(), "cid", false)
	require.NoError(t, err)
	t.Cleanup(func() { _ = logs.Close() })

	out, err := io.ReadAll(logs)
	require.NoError(t, err)
	assert.Equal(t, "stdout line\nstderr line\n", string(out))
}

func TestLogs_CloseTerminatesTheCopier(t *testing.T) {
	// A reader that blocks forever stands in for a followed log stream.
	blocking := &blockingReader{done: make(chan struct{})}
	api := &stubAPI{logsRes: blocking}

	logs, err := newTestAdapter(api).Logs(context.Background(), "cid", true)
	require.NoError(t, err)
	require.NoError(t, logs.Close())
	require.NoError(t, logs.Close(), "Close must be idempotent")

	assert.True(t, blocking.closed(), "closing the demultiplexer must close the source")
}

type blockingReader struct {
	done chan struct{}
}

func (b *blockingReader) Read([]byte) (int, error) {
	<-b.done
	return 0, io.EOF
}

func (b *blockingReader) Close() error {
	select {
	case <-b.done:
	default:
		close(b.done)
	}
	return nil
}

func (b *blockingReader) closed() bool {
	select {
	case <-b.done:
		return true
	default:
		return false
	}
}

func writeLogFrame(w io.Writer, stream byte, payload string) {
	header := make([]byte, 8)
	header[0] = stream
	binary.BigEndian.PutUint32(header[4:], uint32(len(payload)))
	_, _ = w.Write(header)
	_, _ = w.Write([]byte(payload))
}

func TestErrorClassification(t *testing.T) {
	tests := []struct {
		name string
		err  error
		kind error
	}{
		{"not found", errdefs.ErrNotFound, ErrNotFound},
		{"conflict", errdefs.ErrConflict, ErrConflict},
		{"already exists", errdefs.ErrAlreadyExists, ErrConflict},
		{"unavailable", errdefs.ErrUnavailable, ErrDaemonUnavailable},
		{"invalid argument", errdefs.ErrInvalidArgument, ErrInvalidSpec},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := wrap("start", "cid", tt.err)
			require.Error(t, err)

			assert.ErrorIs(t, err, tt.kind, "callers match on the kind")
			assert.ErrorIs(t, err, tt.err, "the underlying cause stays reachable")
			assert.Contains(t, err.Error(), "start")
			assert.Contains(t, err.Error(), "cid")
		})
	}
}

func TestWrap_PassesThroughNilAndLeavesUnknownErrorsUnclassified(t *testing.T) {
	assert.NoError(t, wrap("start", "cid", nil))

	cause := errors.New("some daemon hiccup")
	err := wrap("stop", "cid", cause)
	require.Error(t, err)

	assert.ErrorIs(t, err, cause)
	for _, kind := range []error{ErrNotFound, ErrConflict, ErrDaemonUnavailable, ErrInvalidSpec} {
		assert.NotErrorIs(t, err, kind)
	}
}

func TestCreate_WrapsDaemonErrors(t *testing.T) {
	api := &stubAPI{returnErr: errdefs.ErrConflict}

	_, err := newTestAdapter(api).Create(context.Background(), CreateSpec{Name: "exec_container-abc"})
	require.Error(t, err)
	assert.ErrorIs(t, err, ErrConflict)

	var dockerErr *Error
	require.ErrorAs(t, err, &dockerErr)
	assert.Equal(t, "create", dockerErr.Op)
	assert.Equal(t, "exec_container-abc", dockerErr.ID)
}
