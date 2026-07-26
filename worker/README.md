# Worker

The worker node executes serverless functions inside gVisor/Docker containers on
behalf of the [router](../router). It accepts execution requests over gRPC,
streams them to a Python executor over a unix domain socket, relays results
back, and reports its own state to the router's registry.

## Quick start

```sh
make tools     # install pinned protoc plugins and golangci-lint
make test      # go test -race ./...
make lint      # golangci-lint
make build     # produce ./go-server
make help      # list every target
```

## Architecture

```
cmd/worker/            entry point: signals, config, logger, app.New, app.Run
internal/
  app/                 composition root — the only place concrete types are constructed
  grpcserver/          gRPC transport: server, ExecutionService handler, status mapping
  execution/           orchestration of one request; depends on no transport types
  container/           container lifecycle: acquire, release, reclaim orphans
  executor/            the unix-socket protocol shared with scripts/executor/app.py
  docker/              the sole boundary to a container runtime
  registry/            reporting worker and executor state to the router
  config/ logging/ clock/
  testutil/            fakes: fakedocker, fakeregistry, fakeexecutor, fakesink
  integration/         end-to-end tests over bufconn
proto/                 generated stubs; regenerate with `make proto`
```

Dependencies point inward and every I/O boundary is an interface, so each layer
can be tested against fakes rather than a live daemon:

| Seam | Interface | Production | Test |
|---|---|---|---|
| Container runtime | `docker.Port` | `docker.MobyAdapter` | `fakedocker.Docker` |
| Router | `registry.Reporter` | `registry.GRPCReporter` | `fakeregistry.Recorder` / `.Server` |
| Executor socket | `executor.Dialer` / `executor.Conn` | `executor.UnixDialer` | `net.Pipe`, `fakeexecutor.Server` |
| Response transport | `execution.Sink` | `grpcserver.streamSink` | `fakesink.Sink` |
| Process boundaries | `app.Deps` | `net.Listen`, moby, gRPC | bufconn + fakes |

`internal/docker` deliberately uses two layers. `docker.Port` is expressed in
this repository's own DTOs and is what every other package depends on;
`mobyAPI`, unexported in `moby.go`, mirrors `*client.Client` exactly and is
pinned by a `var _ mobyAPI = (*client.Client)(nil)` assertion. `moby/moby/client`
is pre-1.0 and its signatures have already changed once, so this keeps the next
change confined to one file.

### Request flow

`execution.Runner.Run` owns one request. Several producers — the executor
response reader, the container log tailer, the stats sampler — publish to a
single channel, and only the goroutine that called `Run` drains it into the
`Sink`. That single-owner rule matters: gRPC streams are not safe for concurrent
sends and corrupt frames rather than failing cleanly when misused.

The pumps are split into two groups by lifetime. The primary group (request and
response) does the work the execution exists for. The side group (logs, stats)
follows open-ended streams that only end when the container does, and is
cancelled as soon as the primary group finishes; waiting on both together would
deadlock.

Cleanup runs through a deferred `Release` reading an outcome variable whose zero
value **destroys** the container. Success has to be recorded explicitly on the
final line, so every early return — including a timeout — fails safe. The
release context is detached with `context.WithoutCancel`, so cleanup survives the
very deadline that triggered it.

### Lifecycle

Startup: connect and ping the container runtime → build the router client →
construct the object graph → **reclaim orphaned containers** → listen →
register services with health `NOT_SERVING` → serve → register with the router
→ health `SERVING`.

Orphan reclamation at startup is the only opportunity to recover containers and
directories left by a process that was killed without warning.

Shutdown unwinds in reverse, each step on its own bounded context, accumulating
failures with `errors.Join` rather than aborting: deregister from the router
*before* draining (so it stops routing here while in-flight work finishes) →
`GracefulStop` with a hard-stop fallback → reclaim containers → close clients.

## Contracts

These are load-bearing and covered by tests that fail loudly if changed.

**Router (`../protos`)** — the protos declare no `package`, so the full method
name is `/ExecutionService/RequestExecution`. `worker_id` must remain `worker-1`
and the advertised `worker_uri` must remain `worker:50052`: the router hardcodes
the former and dials the latter verbatim. The first response must carry
`worker_id`, `container_id`, `checkpoint_id`, `cpu_alloc` and `gpu_alloc`.

**Executor (`../scripts/executor/app.py`)** — the executor is the socket server,
binding `$EXECUTOR_DIR/executor.sock`, which the worker sees at
`$WORKER_DIR/<containerID>/executor.sock`. A request is raw cloudpickle bytes in
1 MiB chunks terminated by the literal `EOF` written on its own; the response is
cloudpickle streamed back before the executor closes the connection.

Two properties of that protocol are worth knowing. The executor sleeps 30
seconds awaiting a checkpoint *before* binding, so `DIAL_TOTAL_TIMEOUT` must
exceed that. And `app.py` strips the terminator with `rstrip(b"EOF")`, which
removes any trailing run of `E`, `O` or `F` — a payload ending in those bytes is
silently truncated. Fixing that needs a length-prefixed framing on both ends.

**Container spec** — image, `EXECUTOR_DIR=/tmp`, `PYTHONPATH=/tmp/site_packages`,
`NetworkMode: none`, the two bind mounts, `PidsLimit 100`, `CPUQuota 50000`, and
`Memory` from `cpu_alloc`. `container.Allocation.CPUAlloc` is a byte count
despite its name; the router sends a memory budget in a field called `cpu_alloc`
and the name is kept for wire compatibility.

## Configuration

Required: `SERVICE_NAME`, `PORT`, `WORKER_DIR`, `ROUTER_URI`,
`SITEPACKAGES_TXT_PATH`.

Optional: `WORKER_ID`, `EXECUTOR_IMAGE`, `CONTAINER_RUNTIME` (`runsc` for
gVisor), `LOG_LEVEL`, `LOG_FORMAT` (`json`/`text`), `APP_ENV`, and duration
overrides `EXECUTION_TIMEOUT`, `DIAL_TOTAL_TIMEOUT`, `RESPONSE_IDLE_TIMEOUT`,
`SHUTDOWN_TIMEOUT`, `CLEANUP_TIMEOUT`, `STATS_INTERVAL`, plus `STREAM_LOGS` and
`STREAM_STATS`.

`SITEPACKAGES_TXT_PATH` points at a file containing the host site-packages
directory. It is written at image build time by `python -c print(...)`, so the
value is trimmed on load — an untrimmed newline produces a malformed bind spec
that the daemon rejects opaquely.

Note that `ListenAddr` (`:PORT`) and `WorkerURI` (`SERVICE_NAME:PORT`) are
separate: the worker binds the former and advertises the latter.

## Known gaps

- **gVisor is not wired up.** `CONTAINER_RUNTIME=runsc` will pass the runtime
  through, but `runsc` must be registered on the Docker *daemon's* host, not
  inside this image.
- **Checkpoint formats do not line up.** The worker uses Docker's CRIU
  checkpoint API while `../scripts` produces runsc checkpoint images. Restores
  currently fall back to a cold start, which is logged as a downgrade.
- **`docker-compose.yml` mounts no Docker socket or shared volume**, so the
  worker cannot reach a daemon or share `$WORKER_DIR` with sibling containers as
  configured.
