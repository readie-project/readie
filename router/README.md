# Router

The router accepts execution requests from clients, decides which worker and
which container runs each one, and relays the request to that worker.

The router is the only component that sees the whole cluster, so it owns three
things that no other component can: the registry of live workers and their
containers, the placement decision, and the mapping from a session to the warm
container that holds the session's Python state.

For the cluster-level view, see [Placement](https://readie.org/docs/architecture/placement)
and [Sessions](https://readie.org/docs/concepts/sessions) on the documentation
site. This README covers the internals of the router package.

```sh
make install   # sync the virtualenv from the lockfile
make test      # unit + in-process gRPC integration tests
make lint type # ruff + mypy --strict
make run       # run the router from source
make help      # list every target
```

## Layout

```
src/readie_router/
├── main.py            entry: signals, config, logger, App
├── app.py             COMPOSITION ROOT - the only place concrete types are built
├── config.py          pydantic-settings
├── logging.py  clock.py  errors.py
├── grpcserver/        server, proxy_service, registry_service, session_gate
├── scheduling/        models, state, scheduler, policy, estimator, reaper
├── workers/           ports, channels (pool), client, prober
└── proto/             generated stubs
```

The layout mirrors the Go worker so that both halves read the same way:
`app.py` <-> `internal/app`, `grpcserver/` <-> `internal/grpcserver`,
`scheduling/` <-> `internal/execution`, `workers/` <-> `internal/registry`, and
`tests/fakes/` <-> `internal/testutil`.

## Design

### Package boundary

`scheduling/` imports nothing from `grpcserver/` and nothing from `grpc`. This
keeps the scheduler unit-testable without an event loop, and a guard test
enforces it.

### Dependency injection

The package has no module-level mutable state. Every collaborator takes its
dependencies through `__init__`, and a guard test walks the package to assert
that no module global holds a `ClusterState`, `Scheduler`, or channel pool.

Seams are `typing.Protocol` rather than ABC, so the consumer declares the
interface structurally. This matters most for `WorkerSelector` and its filters,
because a deployment can compose a different placement policy without
subclassing. It also matters for `ExecutionClient`, which an adapter over a
generated stub satisfies.

### Concurrency

`ClusterState` and `Scheduler` contain no `async def` and never await. On a
single-threaded event loop, every method is therefore atomic. A guard test
enforces this, because an `async def` added there would silently reintroduce a
read-modify-write race.

Locks live only outside the domain: in the session gate, the channel pool's
eviction path, the reaper, and the prober. The reaper and prober re-validate
after awaiting instead of writing back a stale read.

### Placement

Placement proceeds in three steps:

1. Affinity. A warm container holds the session's Python state, and moving the
   session loses that state, so a session with a live container stays with it.
2. Filters. A worker must be `READY` and live, have memory headroom, and be
   under its executor cap.
3. Score. The scorer avoids a GPU worker for a non-GPU request, then prefers lower memory
   pressure, then lower CPU pressure, then fewer in-flight requests. The final
   tiebreak is the lexicographic worker id.

Placement uses no randomness, so the same cluster state always yields the same
decision.

### Provision and reconcile

Two concurrent requests for one session would otherwise both see an empty
affinity and both cold-start, and whichever response landed last would win.
Three mechanisms prevent this together:

- `provision` is one synchronous transaction that decides and reserves, so the
  next caller sees the load.
- Reconciliation is idempotent and keyed by `request_id`. It accepts both the
  `PostExecutorStatus(BUSY)` path and the first-response path. Both are needed
  because a silent execution never sends a payload, so it never reveals its
  container through the stream.
- The session gate serializes concurrent requests for one session.

### Session serialization

Session serialization is not a policy choice. A container is one Python
interpreter behind one unix socket and cannot serve two executions at once, so
binding a session to a container forces the calls in that session to queue. The
client SDK documents this in the `Session` docstring, and sessions are opt-in
for that reason.

### Empty session id

An empty `session_id` means that the call is not part of any session. It does
not mean "invalid" or "a session of one".

The client sends `""` for every call without an explicit session. Most calls
never opt into a session, and treating each as a singleton session would track
one `SessionRecord` per request indefinitely. The router therefore handles `""`
in three places:

- `touch_session` refuses to create a record for `""`.
- `provision` skips affinity resolution and binding for `""`.
- `RequestExecution` skips the session gate for `""`. Otherwise every unrelated
  call with an empty session would share one lock key and serialize against the
  others.

### Liveness

The router probes each worker every `PROBE_INTERVAL` over the pooled channel
with `grpc.health.v1`. A worker is evicted after `PROBE_FAILURE_THRESHOLD`
consecutive failures. The router probes actively instead of relying on a
last-seen TTL, because an idle worker emits nothing and a TTL alone would evict
healthy workers.

A separate reaper sweeps TTLs for workers, executors, and leases. This is a
backstop for clients and workers that do not clean up after themselves.

### Session lifetime and tombstones

Sessions have no TTL or cap of their own. When a session's container is gone,
the router keeps the `SessionRecord` and marks it `expired` instead of removing
it. This marker is a tombstone. The container is gone in any of three cases:

- The worker reports `STATUS_REMOVED`, typically because the container's idle
  TTL (`SANDBOX_IDLE_TTL`) expired. See the Sandbox lifecycle section of the
  [worker README](../worker/README.md).
- The router reclaims the container after a lost removal notification.
- The worker that holds the container is evicted. Every container on that worker
  is gone, so `evict_worker` retires its sessions the same way
  `remove_executor` retires the sessions of one container.

`Scheduler.provision` checks the `expired` flag before doing anything else. If a
later request reuses the id, `provision` raises `SessionExpiredError`, which the
server maps to `NOT_FOUND` and the client raises as its own `SessionExpiredError`.
Without the tombstone, the router would cold-start the call under an id that
looks like it should still carry warm state.

Tombstones are kept indefinitely instead of being reaped on a TTL. This is safe
because only explicitly opened sessions are tracked (see Empty session id), so
the record count scales with the number of sessions an application opens, not
with request volume.

A session that has never had a container, or whose affinity was only unpinned
(for example after `STATUS_ERROR`), is not expired. A request for such a session
cold-starts, exactly as it does for an id the router has never seen.

## Contracts

### Bare service names

The protos declare no `package`, so the methods are
`/ProxyService/RequestExecution` and `/RegistryService/…`. The compose
healthcheck runs `grpcurl … grpc.health.v1.Health/Check` against server
reflection, and the worker dials the bare names as well. An integration test
asserts that reflection still advertises them.

### Status enum gap

The `Status` enum has a gap at 1 (`UNKNOWN=0, READY=2, BUSY=3, ERROR=4,
REMOVED=5`). A unit test asserts the gap, because renumbering to close it would
silently reinterpret every status already on the wire.

### Resource budgets

Requests carry resource `budgets`: a `ResourceBudget` per kind (memory and GPU
memory), in bytes. The scheduler reserves and filters on the memory budget,
which is the only resource for which the cluster reports capacity, and forwards
every budget to the worker. `ResourceHeadroomFilter` is generic over kind, so
adding GPU-memory placement requires one filter and a capacity field.

### Health

Health starts as `NOT_SERVING` and changes only when the router can serve, so an
orchestrator never routes to a half-initialized process. On shutdown, health
changes to `NOT_SERVING` before the drain, so a load balancer stops sending new
work while in-flight executions finish.

The drain runs only because `main.py` installs SIGTERM and SIGINT handlers.
Without them, `docker stop` sends SIGKILL and the graceful stop never runs.

## Configuration

The router reads settings from the environment through pydantic-settings, using
the field names below in upper case. A `.env` file is honored if present. For the
full variable reference, see
[Configuration reference](https://readie.org/docs/contributing/configuration-reference).

| Variable | Description |
| --- | --- |
| `BIND_HOST`, `PORT` | Address the server binds. |
| `SERVICE_NAME` | Name the router advertises to workers. |
| `MAX_CONCURRENT_RPCS`, `MAX_MESSAGE_BYTES` | Inbound gRPC limits. |
| `DEFAULT_MEMORY`, `MEMORY_HEADROOM` | Placement. `DEFAULT_MEMORY` is the memory budget for a request that sets none. `MEMORY_HEADROOM` is the fraction of a worker's memory that the router commits. |
| `CATALOGUE_DIR` | Directory of per-flavor `<flavor>.json` catalogues. If absent, every request cold-starts. |
| `AUTH_TOKEN` | Bearer token required on ProxyService. If unset, authentication is disabled. |
| `SESSION_WAIT_TIMEOUT`, `EXECUTION_TIMEOUT` | Per-request bounds. |
| `PROBE_INTERVAL`, `PROBE_TIMEOUT`, `PROBE_FAILURE_THRESHOLD` | Liveness probing. |
| `REAPER_INTERVAL`, `WORKER_TTL`, `EXECUTOR_TTL`, `EXECUTOR_ERROR_TTL` | Eviction of workers, executors, and leases. Sessions have no TTL of their own (see Session lifetime and tombstones). |
| `SHUTDOWN_GRACE` | Drain budget on SIGTERM. |
| `LOG_LEVEL`, `LOG_FORMAT` | Logging. `LOG_FORMAT` is `json` or `console`. |

`BIND_HOST` and `SERVICE_NAME` are separate settings. `BIND_HOST` is the
interface the server listens on, and `SERVICE_NAME` is the hostname that peers
use to reach the router. Binding a hostname such as `router:50051` works only
where that name resolves to a local interface.

## Persistence

Cluster state is held in memory. After a restart, sessions are lost and their
warm containers are reclaimed by the workers' own TTLs. Workers re-register on
their next status report. This is an accepted trade-off: persisting state would
put a datastore on the hot path of every placement decision.
