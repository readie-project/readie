# Worker

The worker is a Go service that executes serverless functions inside gVisor
sandboxes on behalf of the [router](../router). It accepts execution requests
over gRPC, streams them to a Python executor over a unix domain socket, relays
the results back, and reports its own state to the router's registry.

The worker drives `runsc` directly. There is no container daemon and no image
registry in the request path.

For the system-level picture, see the
[architecture overview](https://readie.org/docs/architecture/overview), the
[container lifecycle](https://readie.org/docs/architecture/container-lifecycle),
and the [executor protocol](https://readie.org/docs/architecture/executor-protocol).

## Quick start

```sh
make tools     # install pinned protoc, protoc plugins, and golangci-lint
make test      # go test -race ./...
make lint      # golangci-lint
make build     # produce ./go-server and ./ocispec
make test-e2e  # real-runsc suite; Linux/amd64 only
make help      # list every target
```

gVisor cannot run on an Apple Silicon Mac. It works by intercepting syscalls,
which emulation interferes with, and the executor rootfs is amd64-only. Code
below the runtime seam is therefore tested against fakes. See
[Testing](#testing) and
[Verification against a real runtime](#verification-against-a-real-runtime).

## Architecture

```text
cmd/worker/            entry point: signals, config, logger, app.New, app.Run
cmd/ocispec/           OCI bundle generator, shared with the offline pipeline
internal/
  app/                 composition root - the only place concrete types are built
  grpcserver/          gRPC transport: server, ExecutionService handler, status mapping
  execution/           orchestration of one request; depends on no transport types
  container/           container lifecycle: acquire, release, reclaim orphans
  executor/            the unix-socket protocol shared with ../executor/
  artifact/            the baked-in rootfs, manifest and checkpoints
  sandbox/             the runtime seam - stdlib only, no runtime knowledge
  runsc/               the only package that knows gVisor exists
  registry/            reporting worker and executor state to the router
  config/ logging/ clock/
  testutil/            fakes: fakesandbox, fakeregistry, fakeexecutor, fakesink
  integration/         end-to-end tests over bufconn
proto/                 generated stubs; regenerate with `make proto`
```

Dependencies point inward, and every I/O boundary is an interface:

| Seam               | Interface                   | Production                 | Test                                |
| ------------------ | --------------------------- | -------------------------- | ----------------------------------- |
| Sandbox runtime    | `sandbox.Port`              | `runsc.Adapter`            | `fakesandbox.Sandbox`               |
| Runtime processes  | `runsc.Runner`              | `runsc.ExecRunner`         | `fakeRunner`, `os/exec` helper      |
| Artifacts          | `container.Artifacts`       | `artifact.Registry`        | real registry over a temp tree      |
| Router             | `registry.Reporter`         | `registry.GRPCReporter`    | `fakeregistry.Recorder` / `.Server` |
| Executor socket    | `executor.Dialer` / `.Conn` | `executor.UnixDialer`      | `net.Pipe`, `fakeexecutor.Server`   |
| Response transport | `execution.Sink`            | `grpcserver.streamSink`    | `fakesink.Sink`                     |
| Process boundaries | `app.Deps`                  | real listener, runsc, gRPC | bufconn + fakes                     |

`internal/sandbox` stays stdlib-only so that every consumer can depend on it
without cost. `internal/runsc` holds `os/exec`, the OCI spec, and process
lifetimes. `container.Manager` has no knowledge of the runtime's name.

## Artifacts

The root filesystem and the checkpoints are baked into the base image that the
worker image is built on, at a compiled-in path. Nothing is mounted:

```text
/var/lib/readie/
├── rootfs/                 the one executor root filesystem
├── manifest.json           runsc_version, spec_fingerprint, executor_argv,
│                           executor_protocol, python_path, overlay, network
└── checkpoints/<id>/       runsc image + meta.json
```

A gVisor checkpoint restores only into the filesystem it was captured from, so
the rootfs and the checkpoints must be paired correctly. The layout guarantees
the pairing: there is one rootfs, and the base image build is the only way to
put a rootfs and checkpoints together.

Because there is one rootfs, checkpoint IDs are globally unique, so a bare ID
such as `checkpoint_1` is unambiguous. For background on checkpoints, see
[Checkpoints](https://readie.org/docs/concepts/checkpoints).

## The two images

The [`Dockerfile`](Dockerfile) in this directory builds only the server.
Everything that a checkpoint's validity is bound to comes from
`readie-worker-base-<flavor>`, which the offline pipeline builds
([`../pipeline/Dockerfile`](../pipeline/Dockerfile)). That includes the pinned
runsc, the rootfs, and the checkpoints themselves.

```text
readie-worker-base-<flavor>   pipeline/Dockerfile --target worker-base
  /usr/local/bin/runsc            pinned GVISOR_RELEASE, same as capture
  /var/lib/readie/rootfs
  /var/lib/readie/manifest.json
  /var/lib/readie/checkpoints/

readie-worker-<flavor>        worker/Dockerfile, context ./worker
  FROM readie-worker-base-<flavor>
  /app/go-server
  /usr/local/bin/grpcurl          the compose healthcheck
```

The split follows what changes when. A new capture rebuilds the base. A code
change rebuilds only the worker image, and that build touches nothing large.

### Redeploying a worker change

```sh
make worker-image        # a Go build and two small layers
docker compose up -d     # recreates the worker on the new readie-worker-<flavor>:latest
```

No re-capture is needed. The base already holds the rootfs and the checkpoints,
and the worker image inherits those layers rather than copying them, so the
build does not touch the approximately 26 GB rootfs. Two conditions apply:

- `readie-worker-base-<flavor>:latest` must be present (`make -C .. worker-base`)
  or pullable.
- The worker comes up with whatever checkpoints that base was built with.

If compose does not notice the new image, add `--force-recreate`.

To change the checkpoints themselves, build a whole generation:

```sh
make generation          # capture -> base -> worker; tags readie-worker-<flavor>:<timestamp>
```

`FROM readie-worker-base-<flavor>:latest` names a tag, not a copy. A
`COPY --from=` instruction produces a fresh layer on every build: two
otherwise-identical worker builds produced different digests for the 25.9 GB
rootfs layer, so a rebuild would upload all of it again. A layer inherited from
a base image has the same digest by definition.

`ocispec` is not in this image. It builds OCI bundles from the same code that
the worker uses, but only the pipeline runs it, so it ships in the pipeline
image.

The build context is `./worker`, so [`.dockerignore`](.dockerignore) in this
directory determines what the build can see.

## Request flow

`execution.Runner.Run` owns one request. The executor response reader and the
stats sampler publish to a single channel, and only the goroutine that called
`Run` drains it into the `Sink`. gRPC streams are not safe for concurrent sends
and corrupt frames rather than failing cleanly, so this single-owner rule is
required.

The worker does not currently run a log tailer. The code never emits log
events, and `STREAM_LOGS` is read but has no effect. The function's captured
output (stdout and stderr) arrives in the executor's result envelope, and the
client prints it after the call finishes. See the
[request lifecycle](https://readie.org/docs/architecture/request-lifecycle).

The pumps are split into two groups by lifetime. The primary group (request and
response) does the work that the execution exists for. The side group (stats)
follows an open-ended stream that ends only when the sandbox does. Waiting on
both groups together would deadlock.

Cleanup runs through a deferred `Release` that reads an outcome variable. The
zero value of that variable destroys the sandbox. Success is recorded on the
final line, so every early return fails safe. The release context is detached
so that cleanup survives the deadline that triggered it.

## Sandbox lifecycle

`Create` writes the OCI bundle and starts nothing. `Start` either cold-starts
(`runsc create`, then `runsc start`) or restores (`runsc restore --detach`).
The split exists because a restore both creates and starts in one call, and
that conflicts with an already-created sandbox.

A failed restore deletes the partial sandbox before returning. Without the
deletion, the manager's immediate cold-start retry fails with "already exists",
and a recoverable downgrade becomes a hard failure.

Startup proceeds in this order:

1. Load artifacts.
2. Build and probe the runtime.
3. Create the router client.
4. Build the object graph.
5. Reclaim orphans.
6. Listen, and serve with health `NOT_SERVING`.
7. Register with the router.
8. Set health to `SERVING`.

Shutdown unwinds in reverse. The worker deregisters before draining, so the
router stops routing to it while in-flight work finishes.

Orphan reclamation enumerates bundle directories rather than asking the
runtime. This needs no listing flag. It also finds sandboxes whose runtime state
was lost but whose bundle survives, which a runtime listing would not report.

### Idle containers

A successful execution leaves its container running and marked idle for reuse
(`Manager.Release` calls `MarkIdle`). The exception is a request with no
`session_id`: the container is destroyed regardless of outcome. An empty
`session_id` means the router never established affinity for the container, so
nothing could resume it by ID. Keeping it would hold its memory for
`SANDBOX_IDLE_TTL` with no caller to reuse it. See the Design section of the
[router README](../router/README.md#design).

The container is left running, not suspended, when it goes idle. `Release` runs
in the background after the RPC returns (`Runner.WaitPendingReleases`) so that a
slow `Stop` cannot inflate response latency. As a consequence, a client can
receive a response and send its session's next request before this worker's
release goroutine has finished.

`Manager.ReapIdleContainers` destroys an idle container once it has been idle
for longer than `SANDBOX_IDLE_TTL` (default 5m). The application calls it on a
fixed 30 s ticker. The timer restarts each time the container goes idle again
after a resume. The timer applies only to session-bound containers, because a
container released without a session is destroyed on its first release. Setting
`SANDBOX_IDLE_TTL` to 0 disables reaping.

The reaper is the only mechanism that ends a container's warm life on a
successful path. The router has no session TTL or cap of its own (see the Design
section of the [router README](../router/README.md#design)). It retires a
session only once this reaper, or the router's lost-notification backstop,
reports the container gone. After that, reusing the `session_id` returns an
error instead of silently cold-starting.

## Contracts

### Router

The protos declare no `package`, so the method is
`/ExecutionService/RequestExecution`. The router indexes this worker by the
`worker_id` it registers with and dials `worker_uri` verbatim. A fleet must
therefore give each worker a distinct id. `WORKER_ID` defaults to `worker-1`
because a single-worker stack needs no configuration.

The first response carries `worker_id`, `container_id`, `checkpoint_id`, and the
resource `budgets` that the container ran with.

Capacity is reported, not discovered. `WORKER_MEM_TOTAL` and
`WORKER_MAX_EXECUTORS` accompany every `PostWorkerStatus`. A
`PostWorkerUtilization` message, sent every `WORKER_UTILIZATION_INTERVAL`,
carries the live memory reservation and executor count. The router scores
placement on those values, so over-reporting capacity makes the router
overcommit this worker, and the result is an OOM-killed sandbox mid-execution.
The budget is configured rather than read from `/proc/meminfo` because the
worker usually runs in a container, where that file describes the host and not
the cgroup of the worker.

### Executor

The executor (`../executor/`) is the socket server. It binds
`$EXECUTOR_DIR/executor.sock`, which the worker sees at
`$WORKER_DIR/<id>/executor.sock` through the sandbox's one bind mount.

Both directions are a sequence of length-prefixed chunks that ends with a
zero-length chunk. The payload is cloudpickle: `{func, args, kwargs}` outbound
and a result envelope inbound. `internal/executor` implements the worker side.
The Python side is in `../executor/`. Both test suites decode
`../executor/tests/data/frames.golden.json`, so the two implementations cannot
drift apart. For the wire format, see the
[executor protocol](https://readie.org/docs/architecture/executor-protocol).

A `{"ok": false}` envelope is not a worker failure: the sandbox ran and the
interpreter is healthy. The worker reports the execution as a success and leaves
the container running, marked idle for reuse. Only the client turns that
envelope into an exception.

The manifest records `executor_protocol`. `internal/artifact` refuses artifacts
whose version this worker does not implement, at load time rather than at
restore time.

### Spec compatibility

The worker and the offline pipeline generate their bundles from the same
`runsc.BuildSpec`, through `cmd/ocispec`. Two independent generators cannot be
kept in agreement by review, and a mismatch makes a restore fail opaquely.
Nothing internal to the runtime call bounds a restore once it starts, so the
failure surfaces only when the caller's own dial budget
(`DIAL_TOTAL_TIMEOUT`) runs out.

`Fingerprint` hashes the properties that a checkpoint is sensitive to:

- args
- `terminal`
- cwd
- `root.readonly`
- the ordered mount list
- namespaces
- cpu and pids limits
- overlay
- network

It deliberately excludes mount sources, the memory limit, and the cgroup path,
so per-request variation and `runtime-spec` upgrades do not invalidate
checkpoints.

### Resource budgets

A request's `container.Allocation` is a set of `ResourceBudget` values in
bytes. The worker enforces the memory budget as the container's `memory.max`.
When the budget names a larger maximum, the worker raises `memory.max` as the
container fills up. Other kinds of budget (GPU memory) are honored as hints. See
the memory auto-expand settings under [Configuration](#configuration).

## Starting without artifacts

A worker whose image was built with no root filesystem still starts. It listens,
serves health as `SERVING`, and registers with the status `STATUS_ERROR` instead
of `STATUS_READY`. The router therefore keeps the worker visible and probed but
never places work on it (`is_selectable` requires READY). Any execution that
reaches the worker anyway is refused with `FailedPrecondition` and a message
that names the missing artifact.

The worker does not exit at startup in this case. A container that exits before
it logs gives an operator no information, and under a restart policy it
crash-loops with no indication of the cause.

Health stays `SERVING` on purpose. With `NOT_SERVING`, the router's prober would
evict the worker after three failed probes and hide the state that the ERROR
registration exists to expose.

Three conditions remain fatal at startup, because each indicates a damaged
artifact rather than an absent one:

- **Checkpoints with no rootfs.** Something assembled half an artifact, and
  restoring any of the checkpoints is undefined. An empty tree is accepted; a
  half-filled one is not.
- **A malformed `manifest.json`**, or one that is missing what a restore needs.
- **An artifact directory that exists but cannot be read.** This indicates a
  permissions or mount fault, and hiding it would strand a worker that should
  have seen its artifacts.

A rootfs with no manifest is not fatal. That is the state of the image before
any checkpoint has been captured, and the worker serves cold starts.

The artifact tree is read once at startup, so a new image requires a new
container.

## Configuration

The following variables are required: `SERVICE_NAME`, `PORT`, `WORKER_DIR`, and
`ROUTER_URI`. For defaults and descriptions of every variable, see the
[configuration reference](https://readie.org/docs/contributing/configuration-reference).

The artifact root is not configurable. It is a compiled-in constant
(`config.ArtifactRoot`, `/var/lib/readie`), because a worker image has exactly
one place for its rootfs and checkpoints, and a settable path allowed the mount
and the expectation to disagree. `artifact.Load` still takes a root as a
parameter, which is how the tests build fixtures in temporary directories.

Capacity:

- `WORKER_MEM_TOTAL`: bytes, or a suffixed size such as `8Gi` (default 4 GiB).
- `WORKER_MAX_EXECUTORS`: 0 means unbounded.
- `WORKER_UTILIZATION_INTERVAL`

Flavor and GPU:

- `WORKER_FLAVOR`: `cpu` or `gpu` (default `cpu`). The worker advertises it to
  the router.
- `WORKER_GPU_TOTAL`: the GPU device memory offered, in bytes or with a suffix
  such as `16Gi`.
- `SANDBOX_GPU`: turns on nvproxy and the NVIDIA sandbox environment. It
  defaults to true when the flavor is `gpu`. It must match what the checkpoints
  were captured under, because it is part of the fingerprint.

Memory auto-expand:

- `DEFAULT_CONTAINER_MEM`: the limit for a request that carries no memory budget
  (default 1 GiB).
- `MEM_GROWTH_THRESHOLD`: the fraction of a container's limit whose use triggers
  a raise (default 0.9).
- `MEM_GROWTH_FACTOR`: the multiplier applied to the limit on a raise (default
  2.0).

The live `memory.max` raise is best-effort. Where cgroup delegation is
unavailable (`SANDBOX_IGNORE_CGROUPS`), the raise applies on the container's
next acquisition, and a one-shot retry with a larger container catches a hard
OOM.

Optional variables:

- `WORKER_ID`, `RUNSC_BINARY`, `RUNSC_ROOT`
- `SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY`, `SANDBOX_PLATFORM`,
  `SANDBOX_IGNORE_CGROUPS`, `SANDBOX_DEBUG`, `SANDBOX_DEBUG_LOG_DIR`
- `CGROUP_PARENT`, `CHECKPOINT_STRICT_COMPAT`
- `LOG_LEVEL`, `LOG_FORMAT`, `APP_ENV`
- Duration overrides: `EXECUTION_TIMEOUT`, `DIAL_TOTAL_TIMEOUT`,
  `RESPONSE_IDLE_TIMEOUT`, `SHUTDOWN_TIMEOUT`, `CLEANUP_TIMEOUT`,
  `CONTAINER_STOP_TIMEOUT`, `RUNSC_COMMAND_TIMEOUT`, `CHECKPOINT_TIMEOUT`,
  `STATS_INTERVAL`, `SANDBOX_IDLE_TTL` (default 5m)
- `STREAM_LOGS` and `STREAM_STATS`. `STREAM_LOGS` has no effect (see
  [Request flow](#request-flow)).

Two settings are coupled and validated together. `SANDBOX_OVERLAY` must be a
`root:` overlay. An `all:` overlay keeps the executor's socket in the overlay's
upper layer, where the worker cannot see it, so every execution fails at dial
time in a way that looks like a dead executor. A `:self` overlay writes into
the rootfs directory that every sandbox shares.

`SANDBOX_HOST_UDS` governs whether a socket bound inside the sandbox is visible
on the host. The executor is a socket server, so a value that forbids this
breaks every execution. The default is `create`.

`SANDBOX_NETWORK=sandbox` also makes the worker build a network namespace, a
veth pair, and host-side NAT for each sandbox. See
[Sandbox networking](#sandbox-networking).

## Sandbox networking

`--network=sandbox` is gVisor's own isolated userspace netstack. It is more
secure than `--network=host`, which gives full passthrough to the worker's own
network namespace and shares it with whatever untrusted code a sandbox runs.
However, `runsc create` and `runsc start` never build networking for `sandbox`
mode themselves.

At sandbox start, the sentry joins the network namespace that the OCI spec's
`NetworkNamespace.Path` names and harvests the non-loopback interfaces that
already exist there. Normally a CNI plugin builds that namespace before a
container runtime invokes `runsc`. This worker has no CNI, so without further
work `sandbox` mode gets an empty, newly created namespace with loopback only
and no route out. That state is silent and can be mistaken for a working
network that happens to be locked down.

`internal/network` is a CNI-shaped alternative. When `SANDBOX_NETWORK=sandbox`,
`container.Manager` asks it for a network namespace before creating each
sandbox and passes the resulting path as `NetworkNamespace.Path`.
`Manager.Destroy` releases the namespace afterward. `CleanupOrphans` releases
namespaces too, because it routes every orphan it finds through `Destroy`.

Namespace paths do not affect the fingerprint. `runsc.Fingerprint` hashes the
presence of a namespace, never its `Path`, so a per-sandbox, per-restore
namespace never invalidates a checkpoint. The feature needs `iproute2` and
`iptables` in the worker's own image (see `Dockerfile`), and `CAP_NET_ADMIN` and
`CAP_NET_RAW` (already implied by `privileged: true` in `docker-compose.yml`).

### Pool pre-warming

The pool is pre-warmed at startup, not built per request. Provisioning runs for
every sandbox once `SANDBOX_NETWORK=sandbox` is set. It is not gated on whether
a given call needs network access, because the worker cannot know that at
container-creation time.

A network namespace, its veth pair, addresses, and route are all derived from
the slot and are independent of the sandbox, so nothing needs to be built inside
the request path. `VethProvisioner.WarmUp` builds every capacity slot's
namespace and veth pair once, before the worker starts serving, together with
the one-time `setupNAT` rules. `Provision` then does only id-to-slot
bookkeeping, with no `ip` call and no netlink round trip. `Release` returns a
slot to the pool for reuse instead of tearing it down, so the next sandbox
assigned that slot gets the same namespace and addresses. Only `Close` deletes
the namespaces. It runs once during worker shutdown, after every container has
been destroyed.

Each slot's one-time setup uses two `ip -batch` invocations instead of nine
sequential `ip` process spawns:

- `hostSetupCommands` creates the netns and veth pair and brings up the
  host-side address. It runs in the current namespace.
- `netnsSetupCommands` sets the sandbox-side address, loopback, and default
  route. It runs through `ip netns exec <slot> ip -batch`.

Each writes its lines to a temporary file. The one-time `setupNAT` rules are not
batched, because `iptables` has no equivalent for the existing per-rule
idempotent check-then-add logic, and the rules run once per worker process.

### Egress restrictions

The interface that all sandbox traffic exits through (`HostIface`, detected from
the default route) is the same interface that the worker uses to reach every
sibling container on its own Docker network. That includes the router, which
has no authentication of its own by default (see
[`SECURITY.md`](../SECURITY.md)). Granting a sandbox network access without
excluding that network would let untrusted code reach the router directly and
bypass the sandbox.

`setupNAT` therefore installs two `DROP` rules before the general egress
`ACCEPT` rule, because iptables evaluates a chain in order and stops at the
first match:

- The host's own subnet (`ifaceSubnet`, read from the address of `HostIface`).
- `169.254.0.0/16`, the link-local range where major cloud providers serve
  their instance metadata endpoint, a common SSRF target.

Determining the host's own subnet fails closed. If the subnet cannot be read,
`WarmUp` fails and worker startup fails, so the worker never stands up
unrestricted egress.

## Checkpoint compatibility

A baked checkpoint restores only into the exact sandbox it was captured under.
At startup, once the runtime and container manager are built, the worker
computes its own spec fingerprint and reads its live `runsc --version`. The
fingerprint comes from `container.CanonicalSpec` and `runsc.Fingerprint`, the
same code through which a real container is fingerprinted. The worker then
compares both values to the manifest that the checkpoints were captured under.
Without this check, a drifted overlay, GPU mode, resource limit, or runsc
upgrade would make every restore fall back to a cold start without notice while
the worker still advertised the checkpoints.

`CHECKPOINT_STRICT_COMPAT` (default true) selects the behavior on a mismatch:

- A strict worker refuses the checkpoints. It drops them so that no doomed
  restore is attempted, logs an ERROR that names the divergence, and serves
  cold starts only.
- A tolerant worker (`CHECKPOINT_STRICT_COMPAT=false`) logs a WARN and keeps the
  checkpoints. Restores might degrade to cold starts.

In both modes the rootfs and manifest stay, so the worker keeps running. Only
the offer of checkpoints changes.

### Resolving a startup mismatch

The ERROR names the part of the fingerprint that diverged. The usual cause is
not a code change. It is a deployment in which the worker's sandbox environment
variables (`SANDBOX_NETWORK`, `SANDBOX_OVERLAY`, `SANDBOX_GPU`) disagree with
the values the pipeline used to capture the checkpoints. Nothing enforces that
the two deployments agree, because each is configured independently, for
example in `docker-compose.yml` and in the pipeline's own config or CLI
overrides.

The defaults agree today:

- `SANDBOX_NETWORK` defaults to `sandbox` on both sides, so an unconfigured
  worker and an unconfigured pipeline agree by construction.
- `SANDBOX_OVERLAY` defaults to `root:memory` on both sides.
- `SANDBOX_GPU` is derived from the flavor on both sides.

The overlay and GPU defaults are maintained independently on each side, not
shared. An explicit override of any of the three on only one side causes drift.

To fix the drift, change the worker's environment to match the pipeline's. The
fingerprint must match the checkpoints that already exist, and re-running the
pipeline to match a worker's environment wastes work if a second worker or the
next redeploy drifts again. Restart the worker after the change. No code change
or image rebuild is required.

### Confirming that a request restored from a checkpoint

To distinguish a restore from a cold start, check the following in order:

- **Worker startup logs.** If every request cold-starts, look for the one-time
  ERROR `"baked checkpoints are incompatible with this worker"`. In strict mode,
  the worker drops every checkpoint at boot, so no restore is attempted for the
  life of that process, regardless of what an individual request asks for.
- **Per-request worker logs.** A restore logs `"container restored from
  checkpoint"` (INFO) and names the checkpoint. A cold start logs one of two
  WARN messages, each naming the underlying error:
  - `"cannot resolve the requested checkpoint; starting cold"`: the requested id
    is not installed on this worker.
  - `"checkpoint restore failed, falling back to a cold start"`: the id was
    valid, but the restore call failed.
- **The wire response, for router-side code.**
  `WorkerExecutionResponse.checkpoint_id` carries the checkpoint that the
  request ran against. An empty value means that the request cold-started. A
  non-empty value names the checkpoint that restored. The router reads it from
  the first response to bind its scheduler state, but does not forward it to the
  client-facing `ClientExecutionResponse`. The value is therefore visible to
  router-side code and logs, not to a `readie` caller.

## Testing

There are four test tiers, and three of them run on macOS.

1. **`fakesandbox`** implements `sandbox.Port`. The whole integration suite
   substitutes at `app.Deps.NewRuntime`, above the adapter, so it never touches
   a process.
2. **`fakeRunner`** records argv, which tests assert with full-slice equality.
   runsc parses flags with stdlib `flag` semantics, so a global flag on the
   wrong side of the subcommand is misread rather than rejected.
3. **`os/exec` helper-process tests** cover `ExecRunner`: exit codes, stderr,
   and a detached grandchild that writes to fd 1 after the helper exits. The
   last case shows that descriptors survive the runner's child, which the
   sandbox log capture depends on and which no fake can check.
4. **`make test-e2e`** runs against real runsc on Linux/amd64
   (`//go:build linux && runsc_e2e`).

For the test seams in more detail, see [Architecture](#architecture).

## Verification against a real runtime

The tests in `internal/runsc/e2e_linux_test.go` check the assumptions below
against a real runsc. They require a Linux/amd64 host with runsc installed
(`make test-e2e`) and do not run in CI or on the development machine. The list
separates what this repository's automated tests have not demonstrated from what
the rest of the system already depends on. Items are ordered by blast radius.

1. **`--host-uds=create`.** The flag name, and that a socket bound inside a
   sandbox is reachable from the host. The executor protocol depends on this:
   the executor is the socket server, and `SANDBOX_HOST_UDS` defaults to
   `create` in both the worker and the pipeline. The pipeline captures its
   checkpoints during the executor's pre-bind sleep, so no pipeline test
   exercises the socket. Only
   `TestE2E_SandboxBoundSocketIsReachableFromTheHost` does. If the flag cannot
   be made to work, the fallback is to invert the socket direction, so that the
   worker listens and the executor connects. That change affects `../executor/`.
2. `runsc restore --detach` exists, and restore blocks without it.
3. `runsc checkpoint --leave-running` exists. If it does not, `Manager.Checkpoint`
   is terminal and must be documented as such.
4. Whether `runsc checkpoint` works on a paused sandbox. The adapter assumes
   that it does not, and it resumes, checkpoints, and pauses again.
5. Whether `runsc update` exists. The adapter assumes that it does not, so
   `Adapter.Update` rewrites the bundle and writes the cgroup itself. If the
   subcommand exists, that code becomes unnecessary.
6. The `runsc events --stats` output shape, and whether gVisor populates a CPU
   counter at all. If it reports zeros, the router sees 0% utilization, and
   there is no inexpensive fix.
7. Descriptors passed to `runsc create` survive its exit into the daemonized
   sandbox.
8. `runsc` accepts an absolute `root.path`. The fallback is a symlink at
   `<bundle>/rootfs`.
9. The real default mount set of `runsc spec`, diffed against `BuildSpec` and
   reconciled before generating any checkpoint worth keeping.

Stats are sampled by one short-lived `runsc events --stats` process per
interval (`STATS_INTERVAL`, default 1 s) instead of a single streaming
`runsc events --interval` process, because the streaming flag is unverified. One
process per second per active execution is affordable. Switching to the
streaming form remains open.

## Known gaps

- **No trigger for taking checkpoints.** `execution.proto` carries
  `checkpoint_id` as an input only, so the router cannot ask for a snapshot.
  `Manager.Checkpoint` exists and is tested, but wiring a policy requires a
  protocol change.
- **The rootfs is not tailored** to the packages that the analysis selects. It
  is the full base image regardless. This is deliberate: a new request
  distribution then regenerates only checkpoints, not tens of gigabytes of
  rootfs.
- **Reaping.** `runsc create` daemonizes a sandbox and a gofer that reparent to
  PID 1. The worker does not reap them, so it must not be PID 1. Compose sets
  `init: true`, and the worker logs a warning at startup if it finds itself as
  PID 1.
