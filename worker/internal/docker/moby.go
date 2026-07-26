package docker

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/moby/moby/api/pkg/stdcopy"
	"github.com/moby/moby/api/types/container"
	"github.com/moby/moby/client"
)

// mobyAPI is the subset of *client.Client this adapter uses.
//
// The compile-time assertion below is the source of truth for these
// signatures. moby/moby/client is pre-1.0 and its method shapes have already
// changed once; when they change again this file stops compiling and nothing
// else in the repository does.
type mobyAPI interface {
	ContainerCreate(ctx context.Context, options client.ContainerCreateOptions) (client.ContainerCreateResult, error)
	ContainerStart(ctx context.Context, containerID string, options client.ContainerStartOptions) (client.ContainerStartResult, error)
	ContainerStop(ctx context.Context, containerID string, options client.ContainerStopOptions) (client.ContainerStopResult, error)
	ContainerRemove(ctx context.Context, containerID string, options client.ContainerRemoveOptions) (client.ContainerRemoveResult, error)
	ContainerPause(ctx context.Context, containerID string, options client.ContainerPauseOptions) (client.ContainerPauseResult, error)
	ContainerUnpause(ctx context.Context, containerID string, options client.ContainerUnpauseOptions) (client.ContainerUnpauseResult, error)
	ContainerUpdate(ctx context.Context, containerID string, options client.ContainerUpdateOptions) (client.ContainerUpdateResult, error)
	ContainerInspect(ctx context.Context, containerID string, options client.ContainerInspectOptions) (client.ContainerInspectResult, error)
	ContainerList(ctx context.Context, options client.ContainerListOptions) (client.ContainerListResult, error)
	ContainerLogs(ctx context.Context, containerID string, options client.ContainerLogsOptions) (client.ContainerLogsResult, error)
	ContainerStats(ctx context.Context, containerID string, options client.ContainerStatsOptions) (client.ContainerStatsResult, error)
	Ping(ctx context.Context, options client.PingOptions) (client.PingResult, error)
	Close() error
}

var _ mobyAPI = (*client.Client)(nil)

// MobyAdapter implements Port against a Docker-compatible daemon.
type MobyAdapter struct {
	cli mobyAPI
	log *slog.Logger
}

var _ Port = (*MobyAdapter)(nil)

// NewMobyAdapter wraps an existing client. The client is an interface so the
// adapter's translation logic can be exercised without a daemon.
func NewMobyAdapter(cli mobyAPI, log *slog.Logger) *MobyAdapter {
	if log == nil {
		log = slog.Default()
	}
	return &MobyAdapter{cli: cli, log: log}
}

// NewMobyFromEnv builds a client from the standard DOCKER_* environment and
// verifies the daemon answers before returning. Failing here rather than on
// first use keeps a misconfigured worker from reporting itself healthy.
func NewMobyFromEnv(ctx context.Context, log *slog.Logger) (*MobyAdapter, error) {
	cli, err := client.New(client.FromEnv)
	if err != nil {
		return nil, fmt.Errorf("create docker client: %w", err)
	}

	adapter := NewMobyAdapter(cli, log)
	if err := adapter.Ping(ctx); err != nil {
		// Closing here avoids leaking the client's idle connections when the
		// caller discards the failed constructor result.
		_ = cli.Close()
		return nil, err
	}
	return adapter, nil
}

// Ping verifies the daemon is reachable.
func (a *MobyAdapter) Ping(ctx context.Context) error {
	if _, err := a.cli.Ping(ctx, client.PingOptions{}); err != nil {
		return wrap("ping", "", err)
	}
	return nil
}

// Close releases the underlying client.
func (a *MobyAdapter) Close() error {
	if err := a.cli.Close(); err != nil {
		return wrap("close", "", err)
	}
	return nil
}

// Create creates a container from spec and returns its identifier.
func (a *MobyAdapter) Create(ctx context.Context, spec CreateSpec) (string, error) {
	pidsLimit := spec.PidsLimit

	res, err := a.cli.ContainerCreate(ctx, client.ContainerCreateOptions{
		Name: spec.Name,
		Config: &container.Config{
			Image: spec.Image,
			Env:   spec.Env,
		},
		HostConfig: &container.HostConfig{
			Runtime:     spec.Runtime,
			NetworkMode: container.NetworkMode(spec.NetworkMode),
			Binds:       spec.Binds,
			Resources: container.Resources{
				Memory:    spec.MemoryBytes,
				CPUQuota:  spec.CPUQuota,
				PidsLimit: &pidsLimit,
			},
		},
	})
	if err != nil {
		return "", wrap("create", spec.Name, err)
	}

	for _, warning := range res.Warnings {
		a.log.Warn("docker reported a warning creating container",
			"container_id", spec.Name, "warning", warning)
	}
	return res.ID, nil
}

// Start starts a container, optionally restoring it from a checkpoint.
//
// Unlike the caller-visible contract, no fallback happens here: a failed
// checkpoint restore is reported as an error and the decision to retry with a
// cold start belongs to the container manager, which can log the downgrade.
func (a *MobyAdapter) Start(ctx context.Context, id string, spec StartSpec) error {
	opts := client.ContainerStartOptions{}
	if spec.CheckpointID != "" {
		opts.CheckpointID = spec.CheckpointID
		opts.CheckpointDir = spec.CheckpointDir
	}

	if _, err := a.cli.ContainerStart(ctx, id, opts); err != nil {
		return wrap("start", id, err)
	}
	return nil
}

// Stop stops a running container, forcing it after timeout.
func (a *MobyAdapter) Stop(ctx context.Context, id string, timeout time.Duration) error {
	opts := client.ContainerStopOptions{}
	if timeout > 0 {
		seconds := int(timeout.Seconds())
		opts.Timeout = &seconds
	}

	if _, err := a.cli.ContainerStop(ctx, id, opts); err != nil {
		return wrap("stop", id, err)
	}
	return nil
}

// Remove deletes a container and its anonymous volumes.
func (a *MobyAdapter) Remove(ctx context.Context, id string, force bool) error {
	_, err := a.cli.ContainerRemove(ctx, id, client.ContainerRemoveOptions{
		Force:         force,
		RemoveVolumes: true,
	})
	if err != nil {
		return wrap("remove", id, err)
	}
	return nil
}

// Pause suspends a container's processes.
func (a *MobyAdapter) Pause(ctx context.Context, id string) error {
	if _, err := a.cli.ContainerPause(ctx, id, client.ContainerPauseOptions{}); err != nil {
		return wrap("pause", id, err)
	}
	return nil
}

// Unpause resumes a paused container.
func (a *MobyAdapter) Unpause(ctx context.Context, id string) error {
	if _, err := a.cli.ContainerUnpause(ctx, id, client.ContainerUnpauseOptions{}); err != nil {
		return wrap("unpause", id, err)
	}
	return nil
}

// Update adjusts a running container's resource limits.
func (a *MobyAdapter) Update(ctx context.Context, id string, spec UpdateSpec) error {
	_, err := a.cli.ContainerUpdate(ctx, id, client.ContainerUpdateOptions{
		Resources: &container.Resources{Memory: spec.MemoryBytes},
	})
	if err != nil {
		return wrap("update", id, err)
	}
	return nil
}

// Inspect returns the current state of a container.
func (a *MobyAdapter) Inspect(ctx context.Context, id string) (Info, error) {
	res, err := a.cli.ContainerInspect(ctx, id, client.ContainerInspectOptions{Size: false})
	if err != nil {
		return Info{}, wrap("inspect", id, err)
	}

	info := Info{
		ID:   res.Container.ID,
		Name: strings.TrimPrefix(res.Container.Name, "/"),
	}
	if res.Container.Config != nil {
		info.Image = res.Container.Config.Image
	}
	if res.Container.HostConfig != nil {
		info.MemoryBytes = res.Container.HostConfig.Memory
	}
	if res.Container.State != nil {
		info.Running = res.Container.State.Running
		info.Paused = res.Container.State.Paused
	}
	return info, nil
}

// List returns every container, running or not, whose name starts with
// namePrefix.
func (a *MobyAdapter) List(ctx context.Context, namePrefix string) ([]Summary, error) {
	// The daemon matches names as regular expressions against the leading "/"
	// it prepends to every container name, so anchor on that.
	filters := make(client.Filters).Add("name", "^/"+namePrefix)

	res, err := a.cli.ContainerList(ctx, client.ContainerListOptions{
		All:     true,
		Filters: filters,
	})
	if err != nil {
		return nil, wrap("list", "", err)
	}

	summaries := make([]Summary, 0, len(res.Items))
	for _, item := range res.Items {
		names := make([]string, 0, len(item.Names))
		for _, name := range item.Names {
			names = append(names, strings.TrimPrefix(name, "/"))
		}
		summaries = append(summaries, Summary{
			ID:    item.ID,
			Names: names,
			State: string(item.State),
		})
	}
	return summaries, nil
}

// Logs returns the container's combined output.
//
// Executors run without a TTY, so the daemon frames stdout and stderr with an
// 8-byte header per chunk. The returned reader is demultiplexed; the previous
// implementation forwarded those header bytes to the router as log text.
func (a *MobyAdapter) Logs(ctx context.Context, id string, follow bool) (io.ReadCloser, error) {
	res, err := a.cli.ContainerLogs(ctx, id, client.ContainerLogsOptions{
		ShowStdout: true,
		ShowStderr: true,
		Follow:     follow,
	})
	if err != nil {
		return nil, wrap("logs", id, err)
	}
	return demultiplex(res), nil
}

// Stats subscribes to resource samples for a container.
func (a *MobyAdapter) Stats(ctx context.Context, id string, stream bool) (StatsStream, error) {
	res, err := a.cli.ContainerStats(ctx, id, client.ContainerStatsOptions{
		Stream: stream,
		// Without a preceding sample the first frame carries no PreCPUStats
		// and CPU utilisation cannot be derived from it.
		IncludePreviousSample: !stream,
	})
	if err != nil {
		return nil, wrap("stats", id, err)
	}
	return &mobyStatsStream{body: res.Body, decoder: json.NewDecoder(res.Body)}, nil
}

// mobyStatsStream decodes the daemon's newline-delimited JSON stats frames.
type mobyStatsStream struct {
	body    io.ReadCloser
	decoder *json.Decoder

	closeOnce sync.Once
	closeErr  error
}

// Recv returns the next sample, or io.EOF once the stream ends. A concurrent
// Close unblocks a pending Recv by closing the underlying body.
func (s *mobyStatsStream) Recv() (Stats, error) {
	var frame container.StatsResponse
	if err := s.decoder.Decode(&frame); err != nil {
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			return Stats{}, io.EOF
		}
		return Stats{}, wrap("stats recv", frame.ID, err)
	}

	return Stats{
		Read:         frame.Read,
		MemoryUsage:  int64(frame.MemoryStats.Usage),
		MemoryLimit:  int64(frame.MemoryStats.Limit),
		CPUTotal:     int64(frame.CPUStats.CPUUsage.TotalUsage),
		CPUSystem:    int64(frame.CPUStats.SystemUsage),
		PreCPUTotal:  int64(frame.PreCPUStats.CPUUsage.TotalUsage),
		PreCPUSystem: int64(frame.PreCPUStats.SystemUsage),
		OnlineCPUs:   int64(frame.CPUStats.OnlineCPUs),
	}, nil
}

// Close releases the stream. It is idempotent and safe to call concurrently
// with Recv.
func (s *mobyStatsStream) Close() error {
	s.closeOnce.Do(func() { s.closeErr = s.body.Close() })
	return s.closeErr
}

// demultiplex converts the daemon's framed log stream into plain bytes.
func demultiplex(src io.ReadCloser) io.ReadCloser {
	pr, pw := io.Pipe()

	go func() {
		// StdCopy writes each frame to one destination at a time, so sharing a
		// single writer for both streams interleaves them without racing.
		_, err := stdcopy.StdCopy(pw, pw, src)
		_ = src.Close()
		_ = pw.CloseWithError(err)
	}()

	return &demuxReader{pipe: pr, src: src}
}

// demuxReader couples the demultiplexed pipe to the raw source, so closing it
// terminates the copying goroutine rather than leaking it.
type demuxReader struct {
	pipe      *io.PipeReader
	src       io.ReadCloser
	closeOnce sync.Once
}

func (d *demuxReader) Read(p []byte) (int, error) { return d.pipe.Read(p) }

func (d *demuxReader) Close() error {
	var err error
	d.closeOnce.Do(func() {
		// Closing the source unblocks StdCopy; closing the pipe unblocks any
		// reader. Both are required to guarantee the goroutine exits.
		srcErr := d.src.Close()
		pipeErr := d.pipe.Close()
		err = errors.Join(srcErr, pipeErr)
	})
	return err
}
