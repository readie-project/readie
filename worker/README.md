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
  artifact/            the baked-in rootfs, manifest and checkpoints
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

## Artifacts

The root filesystem and the checkpoints are **baked into the base image** this one
is built on, at a compiled-in path. Nothing is mounted:

```
/var/lib/crfs/
├── rootfs/                 the one executor root filesystem
├── manifest.json           runsc_version, spec_fingerprint, executor_argv,
│                           executor_protocol, python_path, overlay, network
└── checkpoints/<id>/       runsc image + meta.json
```

**A gVisor checkpoint only restores into the filesystem it was captured from**,
so the pairing has to be right. It is right by construction: there is one rootfs,
and the base image build is the only way to put a rootfs and checkpoints together.
That replaced a `rootfs_id` recorded in every artifact — which was never actually
compared, only logged.

One rootfs also means checkpoint IDs are globally unique, so `checkpoint_1` is
unambiguous. The previous layout nested them under `generations/<id>/` and needed
a `gen-2026-06/checkpoint_1` reference to disambiguate.

## The two images

[`Dockerfile`](Dockerfile) here builds only the server. Everything a checkpoint's
validity is bound to — the pinned runsc, the rootfs, the checkpoints themselves —
comes from `crfs-worker-base`, which the offline pipeline builds
([`../pipeline/Dockerfile`](../pipeline/Dockerfile)).

```
crfs-worker-base            pipeline/Dockerfile --target worker-base
  /usr/local/bin/runsc            pinned GVISOR_RELEASE, same as capture
  /var/lib/crfs/rootfs
  /var/lib/crfs/manifest.json
  /var/lib/crfs/checkpoints/

crfs-worker                 worker/Dockerfile, context ./worker
  FROM crfs-worker-base
  /app/go-server
  /usr/local/bin/grpcurl          the compose healthcheck
```

The split is along what changes when. A new capture rebuilds the base; a code
change rebuilds only this image, and that build reaches nothing large:

### Redeploying a worker change

```sh
make worker-image        # a Go build and two small layers; seconds
docker compose up -d     # recreates the worker on the new crfs-worker:latest
```

No re-capture. The base already holds the rootfs and the checkpoints, and they are
*inherited* rather than copied, so nothing here touches 26 GB. Two conditions:
`crfs-worker-base:latest` has to be present (`make -C .. worker-base`) or
pullable, and the worker comes up with whatever checkpoints that base was built
with. If compose does not notice the new image, add `--force-recreate`.

A whole generation, when the checkpoints themselves should change:

```sh
make generation          # capture -> base -> worker; tags crfs-worker:<timestamp>
```

`FROM crfs-worker-base:latest` names a *tag*, and a tag rather than a copy is the
whole point. `COPY --from=` produces a fresh layer every build: two
otherwise-identical worker builds were observed to differ in the 25.9 GB rootfs
layer's digest, so a rebuild would have re-uploaded all of it. A layer inherited
from a base image has the same digest by definition.

`ocispec` is **not** in this image. It builds OCI bundles from the same code the
worker uses, but only the pipeline ever runs it, so it ships in the pipeline image
instead.

The build context is `./worker`, so [`.dockerignore`](.dockerignore) here is
load-bearing — anything it lists is invisible to the build.

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

The manifest records `executor_protocol`, and `internal/artifact` refuses
artifacts whose version this worker does not implement, at load rather than at
restore.

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

A worker whose image was built with no root filesystem still starts. It listens,
serves health as SERVING, and registers — but as `STATUS_ERROR` rather than
`STATUS_READY`, so the router keeps it visible and probed while never placing
work on it (`is_selectable` requires READY). Any execution that reaches it
anyway is refused with `FailedPrecondition` and a message naming the missing
artifact.

Exiting at startup instead was worse in practice: a container that dies before
it logs tells an operator nothing, and under a restart policy it crash-loops
with no indication which of a dozen causes applies.

Health stays SERVING deliberately. NOT_SERVING would have the router's prober
evict the worker after three failed probes, hiding the very state the ERROR
registration exists to expose.

Three things are still fatal at startup, because none of them is simply an
absent artifact:

- **Checkpoints with no rootfs.** Something assembled half an artifact, and
  restoring any of them is undefined. An empty tree is fine; a half-filled one
  is not.
- **A malformed `manifest.json`**, or one missing what a restore needs.
- **An artifact directory that exists but cannot be read** — a permissions or
  mount fault, where hiding it would strand a worker that should have seen its
  artifacts.

A rootfs with no manifest is *not* fatal: that is what the image ships with
before any checkpoint has been captured, and it serves cold starts.

The artifact tree is read once at startup, so a new image means a new container.
That is the point of baking them in.

## Configuration

Required: `SERVICE_NAME`, `PORT`, `WORKER_DIR`, `ROUTER_URI`.

The artifact root is **not** configurable: it is a compiled-in constant
(`config.ArtifactRoot`, `/var/lib/crfs`) because there is one place a worker
image puts its rootfs and checkpoints, and a settable path invited a deployment
where the mount and the expectation disagreed. `artifact.Load` still takes a
root as a parameter, which is how the tests build fixtures in temp directories.

Capacity: `WORKER_MEM_TOTAL` (bytes, or a suffixed size such as `8Gi`;
default 4 GiB), `WORKER_MAX_EXECUTORS` (0 meaning unbounded),
`WORKER_UTILIZATION_INTERVAL`.

Optional: `WORKER_ID`, `RUNSC_BINARY`, `RUNSC_ROOT`,
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
