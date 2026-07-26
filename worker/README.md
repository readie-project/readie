# Worker

The worker node executes serverless functions inside gVisor sandboxes on behalf
of the [router](../router). It accepts execution requests over gRPC, streams
them to a Python executor over a unix domain socket, relays results back, and
reports its own state to the router's registry.

It drives `runsc` directly. There is no container daemon and no image registry
in the request path.

## Quick start

```sh
make tools     # install pinned protoc plugins and golangci-lint
make test      # go test -race ./...
make lint      # golangci-lint
make build     # produce ./go-server and ./ocispec
make test-e2e  # real-runsc suite; Linux/amd64 only
make help      # list every target
```

**gVisor cannot run on an Apple Silicon Mac.** It works by intercepting
syscalls, which is precisely what emulation sits in the middle of, and the
executor rootfs is amd64-only. Everything below the runtime seam is therefore
tested against fakes; see [Testing](#testing) and
[Unverified against a real runtime](#unverified-against-a-real-runtime).

## Architecture

```
cmd/worker/            entry point: signals, config, logger, app.New, app.Run
cmd/ocispec/           OCI bundle generator, shared with the offline pipeline
internal/
  app/                 composition root — the only place concrete types are built
  grpcserver/          gRPC transport: server, ExecutionService handler, status mapping
  execution/           orchestration of one request; depends on no transport types
  container/           container lifecycle: acquire, release, reclaim orphans
  executor/            the unix-socket protocol shared with ../executor/
  artifact/            generations: rootfs + the checkpoints captured against it
  sandbox/             the runtime seam — stdlib only, no runtime knowledge
  runsc/               the only package that knows gVisor exists
  registry/            reporting worker and executor state to the router
  config/ logging/ clock/
  testutil/            fakes: fakesandbox, fakeregistry, fakeexecutor, fakesink
  integration/         end-to-end tests over bufconn
proto/                 generated stubs; regenerate with `make proto`
```

Dependencies point inward and every I/O boundary is an interface:

| Seam | Interface | Production | Test |
|---|---|---|---|
| Sandbox runtime | `sandbox.Port` | `runsc.Adapter` | `fakesandbox.Sandbox` |
| Runtime processes | `runsc.Runner` | `runsc.ExecRunner` | `fakeRunner`, `os/exec` helper |
| Artifacts | `container.Artifacts` | `artifact.Registry` | real registry over a temp tree |
| Router | `registry.Reporter` | `registry.GRPCReporter` | `fakeregistry.Recorder` / `.Server` |
| Executor socket | `executor.Dialer` / `.Conn` | `executor.UnixDialer` | `net.Pipe`, `fakeexecutor.Server` |
| Response transport | `execution.Sink` | `grpcserver.streamSink` | `fakesink.Sink` |
| Process boundaries | `app.Deps` | real listener, runsc, gRPC | bufconn + fakes |

`internal/sandbox` stays stdlib-only so every consumer can depend on it for
free; `internal/runsc` holds `os/exec`, the OCI spec, and process lifetimes.
`container.Manager` never learns the runtime's name.

## Generations

One run of the offline pipeline produces one immutable artifact:

```
$ARTIFACT_ROOT/generations/<generationID>/
├── generation.json     id, rootfs_id, runsc_version, spec_fingerprint, …
├── rootfs/             the executor root filesystem
└── checkpoints/<id>/   runsc image + meta.json
```

Pairing them is not a convenience. **A gVisor checkpoint only restores into the
filesystem it was captured from**, so a checkpoint that travels without its
rootfs is unusable. A worker may hold several generations at once; each
checkpoint names its owner, and the rootfs a restore runs against is looked up
*through* the checkpoint rather than assumed. That is what makes a mismatched
pair impossible rather than merely detected.

A bare `checkpoint_1` resolves to the newest generation containing it; a
qualified `gen-2026-06/checkpoint_1` pins one exactly. Cold starts use the
active generation (newest by default, `ACTIVE_GENERATION` to pin). A generation
whose rootfs is missing is logged and skipped rather than failing startup — one
bad artifact should not take a worker offline when others are serviceable.

Building and installing one:

```sh
docker build -t crfs-executor-rootfs:<tag> -f rootfs/Dockerfile .
docker create --name export crfs-executor-rootfs:<tag>
mkdir -p /var/lib/crfs/generations/<gen>/rootfs
docker export export | tar -x -C /var/lib/crfs/generations/<gen>/rootfs
docker rm export
# then copy generation.json and checkpoints/ from the scripts pipeline
```

## Request flow

`execution.Runner.Run` owns one request. Several producers — the executor
response reader, the log tailer, the stats sampler — publish to a single
channel, and only the goroutine that called `Run` drains it into the `Sink`.
gRPC streams are not safe for concurrent sends and corrupt frames rather than
failing cleanly, so that single-owner rule is load-bearing.

The pumps are split into two groups by lifetime: the primary group does the
work the execution exists for, while logs and stats follow open-ended streams
that only end when the sandbox does. Waiting on both together would deadlock.

Cleanup runs through a deferred `Release` reading an outcome variable whose
zero value **destroys** the sandbox. Success is recorded on the final line, so
every early return fails safe, and the release context is detached so cleanup
survives the deadline that triggered it.

## Sandbox lifecycle

`Create` writes the OCI bundle and starts nothing; `Start` either cold-starts
(`runsc create` then `runsc start`) or restores (`runsc restore --detach`).
That split exists because a restore both creates and starts in one call and
conflicts with an already-created sandbox.

A failed restore deletes the partial sandbox before returning. This is not
optional: without it the manager's immediate cold-start retry hits "already
exists" and a recoverable downgrade becomes a hard failure.

Startup: load artifacts → build and probe the runtime → router client → object
graph → **reclaim orphans** → listen → serve with health `NOT_SERVING` →
register with the router → health `SERVING`. Shutdown unwinds in reverse,
deregistering *before* draining so the router stops routing here while
in-flight work finishes.

Orphan reclamation enumerates bundle directories rather than asking the
runtime. That needs no listing flag and, more importantly, finds sandboxes
whose runtime state was lost but whose bundle survives — exactly the case
cleanup exists for, and one a runtime listing would not mention.

## Contracts

**Router** — the protos declare no `package`, so the method is
`/ExecutionService/RequestExecution`. The router indexes this worker by whatever
`worker_id` it registers with and dials `worker_uri` verbatim, so a fleet must
give each worker a distinct id; `WORKER_ID` defaults to `worker-1` only because
a single-worker stack needs no configuration. (An earlier router hardcoded
`worker-1` in `provision()` and that constraint was real — it no longer is.)
The first response carries `worker_id`, `container_id`, `checkpoint_id`,
`cpu_alloc` and `gpu_alloc`.

Capacity is reported rather than discovered. `WORKER_MEM_TOTAL` and
`WORKER_MAX_EXECUTORS` ride on every `PostWorkerStatus`, and a
`PostWorkerUtilization` every `WORKER_UTILIZATION_INTERVAL` carries live memory
reservation and executor count. The router scores placement on those, so
over-reporting capacity makes it overcommit this worker and the resulting
failure is an OOM-killed sandbox mid-execution. The budget is configured rather
than read from `/proc/meminfo` because the worker is usually containerised,
where that file describes the host and not the cgroup the worker lives in.

**Executor** (`../executor/`) — the executor is the socket
server, binding `$EXECUTOR_DIR/executor.sock`, which the worker sees at
`$WORKER_DIR/<id>/executor.sock` through the sandbox's one bind mount.

Both directions are a sequence of length-prefixed chunks ending in a
zero-length one, carrying cloudpickle: `{func, args, kwargs}` out, a result
envelope back. `internal/executor` implements this side; the Python side is in
`../executor/`, and `../executor/tests/data/frames.golden.json` is decoded by
both test suites so the two cannot drift.

Two properties matter. The executor sleeps 30 seconds awaiting a checkpoint
*before* binding, so `DIAL_TOTAL_TIMEOUT` must exceed it. And a `{"ok": false}`
envelope is *not* a worker failure — the sandbox ran and the interpreter is
healthy — so the execution is reported as a success and the container is paused
for reuse. Only the client turns that envelope into an exception.

A generation records `executor_protocol`, and `internal/artifact` refuses one
whose version this worker does not implement, at load rather than at restore.

**Spec compatibility** — the worker and the offline pipeline generate their
bundles from the same `runsc.BuildSpec`, via `cmd/ocispec`. Two independent
generators cannot be kept in agreement by review, and a mismatch fails a restore
opaquely 30 seconds into a request. `Fingerprint` hashes what a checkpoint is
sensitive to — args, `terminal`, cwd, `root.readonly`, the ordered mount list,
namespaces, cpu/pids limits, overlay and network — and deliberately excludes
mount sources, the memory limit and the cgroup path, so per-request variation
and `runtime-spec` upgrades do not invalidate checkpoints.

Note `container.Allocation.CPUAlloc` is a byte count despite its name: the
router sends a memory budget in a field called `cpu_alloc`, and the name is kept
for wire compatibility.

## Starting without artifacts

A worker whose `ARTIFACT_ROOT` holds no usable generation still starts. It
listens, serves health as SERVING, and registers — but as `STATUS_ERROR` rather
than `STATUS_READY`, so the router keeps it visible and probed while never
placing work on it (`is_selectable` requires READY). Any execution that reaches
it anyway is refused with `FailedPrecondition` and a message naming the missing
artifact.

Exiting at startup instead was worse in practice: a container that dies before
it logs tells an operator nothing, and under a restart policy it crash-loops
with no indication which of a dozen causes applies.

Health stays SERVING deliberately. NOT_SERVING would have the router's prober
evict the worker after three failed probes, hiding the very state the ERROR
registration exists to expose.

Two things are still fatal at startup, because neither is a missing artifact:
an `ACTIVE_GENERATION` that names a generation which is not present (a
configuration typo — starting on a different one than asked for would be
worse), and an artifact directory that exists but cannot be read (a permissions
or mount fault, where hiding it would strand a worker that should have seen its
artifacts).

Installing a generation requires a restart; the registry is read once at
startup.

## Configuration

Required: `SERVICE_NAME`, `PORT`, `WORKER_DIR`, `ROUTER_URI`, `ARTIFACT_ROOT`.

Capacity: `WORKER_MEM_TOTAL` (bytes, or a suffixed size such as `8Gi`;
default 4 GiB), `WORKER_MAX_EXECUTORS` (0 meaning unbounded),
`WORKER_UTILIZATION_INTERVAL`.

Optional: `WORKER_ID`, `ACTIVE_GENERATION`, `RUNSC_BINARY`, `RUNSC_ROOT`,
`SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY`, `SANDBOX_PLATFORM`,
`SANDBOX_IGNORE_CGROUPS`, `SANDBOX_DEBUG`, `SANDBOX_DEBUG_LOG_DIR`,
`CGROUP_PARENT`, `CHECKPOINT_STRICT_COMPAT`, `LOG_LEVEL`, `LOG_FORMAT`,
`APP_ENV`, plus duration overrides `EXECUTION_TIMEOUT`, `DIAL_TOTAL_TIMEOUT`,
`RESPONSE_IDLE_TIMEOUT`, `SHUTDOWN_TIMEOUT`, `CLEANUP_TIMEOUT`,
`CONTAINER_STOP_TIMEOUT`, `RUNSC_COMMAND_TIMEOUT`, `RESTORE_TIMEOUT`,
`CHECKPOINT_TIMEOUT`, `STATS_INTERVAL`, and `STREAM_LOGS` / `STREAM_STATS`.

Two settings are coupled and validated together: `SANDBOX_OVERLAY` must be a
`root:` overlay. An `all:` overlay would keep the executor's socket in the
overlay's upper layer where the worker cannot see it — every execution would
fail at dial time looking exactly like a dead executor — and `:self` writes into
the rootfs directory every sandbox shares.

`SANDBOX_HOST_UDS` governs whether a socket bound inside the sandbox is visible
on the host. The executor is a socket server, so a value that forbids it breaks
every execution.

## Testing

Four tiers; three run on macOS.

1. **`fakesandbox`** implements `sandbox.Port`. The whole integration suite substitutes at `app.Deps.NewRuntime`, above the adapter, so it never touches a process.
2. **`fakeRunner`** records argv, asserted with full-slice equality — runsc parses flags with stdlib `flag` semantics, so a global flag on the wrong side of the subcommand is misread rather than rejected.
3. **`os/exec` helper-process tests** for `ExecRunner`: exit codes, stderr, and a detached grandchild that writes to fd 1 *after* the helper exits. That last one proves descriptors survive the runner's child — the assumption the entire log implementation rests on, and one no fake can check.
4. **`make test-e2e`** — real runsc, Linux/amd64, `//go:build linux && runsc_e2e`.

## Unverified against a real runtime

Everything below could not be checked on the development machine. Each has a
test in `internal/runsc/e2e_linux_test.go`; when one passes, delete its line
here. Ordered by blast radius.

1. **`--host-uds=create`** — that the flag exists with that name and that a socket bound inside a sandbox is genuinely reachable from the host. Nothing in this repository has ever demonstrated it: the pipeline captures its checkpoints during the executor's pre-bind sleep. If it cannot be made to work, the fallback is to invert the socket direction — the worker listens, the executor connects — which changes `../executor/`. **Check this first.**
2. `runsc restore --detach` exists, and restore blocks without it.
3. `runsc checkpoint --leave-running` exists. If not, `Manager.Checkpoint` is terminal and must be documented so.
4. Whether `runsc checkpoint` works on a paused sandbox. The adapter assumes not, and resumes → checkpoints → re-pauses.
5. Whether `runsc update` exists. If it does, `Adapter.Update`'s bundle rewrite and cgroup write become unnecessary.
6. The `runsc events --stats` output shape, and **whether gVisor populates a CPU counter at all**. If it reports zeros the router sees 0% utilisation and there is no cheap fix.
7. That descriptors passed to `runsc create` survive its exit into the daemonised sandbox.
8. That `runsc` accepts an absolute `root.path`. Fallback: symlink `<bundle>/rootfs`.
9. `runsc spec`'s real default mount set, diffed against `BuildSpec` and reconciled **before** generating any checkpoint worth keeping.

Also unresolved: stats are sampled by one short-lived `runsc events --stats`
per interval rather than a single streaming `runsc events --interval` process,
because the streaming flag is unverified. One fork per second per active
execution is affordable; switching is a follow-up.

## Known gaps

- **No trigger for taking checkpoints.** `execution.proto` carries `checkpoint_id` as an input only, so the router cannot ask for a snapshot. `Manager.Checkpoint` exists and is tested; wiring a policy needs a protocol change.
- **The rootfs is not tailored** to the packages the analysis selects — it is the full base image regardless. That is deliberate: a new request distribution then regenerates only checkpoints, not tens of gigabytes of rootfs.
- **Reaping.** `runsc create` daemonises a sandbox and a gofer that reparent to PID 1. The worker does not reap them, so it must not be PID 1; compose sets `init: true` and the worker warns at startup if it finds itself as PID 1.
