# Router

Accepts execution requests from clients, decides which worker and which container
should run them, and relays the request through to that worker.

It is the only component that sees the whole cluster, so it owns three things no
one else can: the registry of live workers and their containers, the placement
decision, and the mapping from a session to the warm container holding that
session's Python state.

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

This deliberately mirrors the Go worker so both halves read the same way:
`app.py` <-> `internal/app`, `grpcserver/` <-> `internal/grpcserver`, `scheduling/`
<-> `internal/execution`, `workers/` <-> `internal/registry`, `tests/fakes/` <->
`internal/testutil`.

## Design

**The load-bearing boundary.** `scheduling/` imports nothing from `grpcserver/`
and nothing from `grpc`. That is what makes the scheduler unit-testable with no
event loop at all, and a guard test enforces it.

**Dependency injection.** There is no module-level mutable state: every
collaborator takes its dependencies through `__init__`, and a guard test walks
the package asserting no module global holds a `ClusterState`, `Scheduler` or
channel pool. Seams are `typing.Protocol` rather than ABC, so the consumer
declares the interface structurally - which matters most for `WorkerSelector`
and its filters (a deployment can compose a different placement policy without
subclassing) and for `ExecutionClient` (satisfied by an adapter over a generated
stub we do not control).

**Concurrency.** `ClusterState` and `Scheduler` contain no `async def` and never
await, so on a single-threaded loop every method is atomic by construction. A
guard test enforces that too, because an `async def` slipped in there silently
reintroduces a read-modify-write race. Locks live only outside the domain: the
session gate, the channel pool's eviction path, and the reaper and prober, which
re-validate after awaiting rather than writing back a stale read.

**Placement** is affinity first - a warm container holds the session's Python
state, and moving it silently loses that - then filters (READY, live, memory
headroom, executor cap) and a score that prefers memory pressure, falls back to
CPU, then to in-flight count, with a lexicographic tiebreak on worker id. No
randomness, so the same cluster state always yields the same decision.

**The provision/reconcile hazard.** Two concurrent requests for one session would
otherwise both see an empty affinity and both cold-start, with whichever response
landed last winning. Three things fix it together: `provision` is one synchronous
transaction that _decides and reserves_, so the next caller sees the load;
reconciliation is idempotent and keyed by `request_id`, accepting both the
`PostExecutorStatus(BUSY)` path and the first-response path - necessary because a
silent execution never sends a payload and so never reveals its container through
the stream; and concurrent requests for one session are serialised by the gate.

**Session serialisation is not a policy choice.** A container is one Python
interpreter behind one unix socket and cannot serve two executions at once, so
binding a session to a container forces the calls in it to queue. The client SDK
says so in `Session`'s docstring, and makes sessions opt-in for that reason.

**An empty `session_id` means "not part of any session", not "invalid" or "a
session of one".** The client sends `""` for every call with no explicit
session, rather than minting a fresh id per call - most calls never opt into
a session at all, so treating each as a singleton session would track one
`SessionRecord` per request forever. `touch_session` refuses to create a
record for `""`, `provision` skips affinity resolution and binding for it
entirely, and `RequestExecution` skips the session gate rather than passing
`""` through it (every unrelated empty-session call would otherwise share
that one lock key and serialise against each other - the exact fleet-wide
contention the per-call unique id used to exist to avoid, just moved to the
gate instead of the scheduler).

**Liveness** is probed every `PROBE_INTERVAL` over the pooled channel via
`grpc.health.v1`, with `PROBE_FAILURE_THRESHOLD` strikes to eviction. Active
probing rather than a last-seen TTL, because an idle worker emits nothing and a
TTL alone would evict healthy workers. A separate reaper sweeps TTLs for
workers, executors and leases: a TTL is a promise about a well-behaved client.

**Sessions carry no TTL or cap of their own, and a dead one is a tombstone,
not a deletion.** The instant the container backing a session is gone -
reported by the worker as `STATUS_REMOVED` (typically its own pause TTL
expiring, see the worker README), reclaimed here after a lost removal
notification, or because the *worker* holding it was evicted (every
container on it is gone too, so `evict_worker` retires those sessions the
same way `remove_executor` does for one container) - the `SessionRecord`
is marked `expired` rather than popped from the map. `Scheduler.provision`
checks that flag before doing anything else and raises `SessionExpiredError`
(-> `NOT_FOUND` -> the client's `SessionExpiredError`) if a later request
reuses that id, rather than silently cold-starting under an id that looks
like it should still carry warm state. Tombstones are kept forever rather
than reaped on a TTL, which is only sound because this applies solely to
genuine, opted-in sessions in the first place - an empty `session_id` is
never tracked at all (above), so the record count here scales with how many
sessions an application deliberately opens, not with request volume. A
session that has never had a container yet (or whose affinity was merely
unpinned, e.g. `STATUS_ERROR`) is not expired - that case still just
cold-starts, exactly as an id the router has never seen at all would.

## Contracts

**Service names are bare.** The protos declare no `package`, so the methods are
`/ProxyService/RequestExecution` and `/RegistryService/…`. The compose
healthcheck shells out to `grpcurl … grpc.health.v1.Health/Check` against server
reflection, and the worker dials the bare names too. An integration test asserts
reflection still advertises them.

**The `Status` enum has a gap at 1** (`UNKNOWN=0, READY=2, BUSY=3, ERROR=4,
REMOVED=5`). A unit test asserts it, because renumbering to close the gap would
silently reinterpret every status already on the wire.

**Requests carry resource `budgets`**, a `ResourceBudget` per kind (memory, GPU
memory) in bytes. The scheduler reserves and filters on the memory budget - the
only resource the cluster reports capacity for - and forwards every budget to
the worker. `ResourceHeadroomFilter` is generic over kind, so adding GPU-memory
placement is one filter plus a capacity field.

**Health starts NOT_SERVING** and flips only once the router can genuinely
serve, so an orchestrator never routes to a half-initialised process. On the way
down it goes NOT_SERVING _before_ the drain, so a load balancer stops sending new
work while in-flight executions finish. That drain only happens because `main.py`
installs SIGTERM and SIGINT handlers; without them `docker stop` SIGKILLs the
process and the graceful stop never runs.

## Configuration

Read from the environment by pydantic-settings, under the field names below
(uppercased). A `.env` file is honoured if present.

|                                                                                      |                                                                                                                          |
| ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------ |
| `BIND_HOST`, `PORT`                                                                  | what the server binds                                                                                                    |
| `SERVICE_NAME`                                                                       | what the router advertises to workers                                                                                    |
| `MAX_CONCURRENT_RPCS`, `MAX_MESSAGE_BYTES`                                           | inbound gRPC limits                                                                                                      |
| `DEFAULT_MEMORY`, `MEMORY_HEADROOM`                                                  | placement - the memory budget for a request that sets none, and the fraction of a worker's memory the router will commit |
| `CATALOGUE_DIR`                                                                      | directory of per-flavor `<flavor>.json` catalogues; absent means cold starts                                             |
| `AUTH_TOKEN`                                                                         | bearer token required on ProxyService; unset means no auth                                                               |
| `SESSION_WAIT_TIMEOUT`, `EXECUTION_TIMEOUT`                                          | per-request bounds                                                                                                       |
| `PROBE_INTERVAL`, `PROBE_TIMEOUT`, `PROBE_FAILURE_THRESHOLD`                         | liveness                                                                                                                 |
| `REAPER_INTERVAL`, `WORKER_TTL`, `EXECUTOR_TTL`, `EXECUTOR_ERROR_TTL`                | eviction (workers, executors, leases - sessions have no TTL of their own, see Design)                                   |
| `SHUTDOWN_GRACE`                                                                     | drain budget on SIGTERM                                                                                                  |
| `LOG_LEVEL`, `LOG_FORMAT`                                                            | `json` or `console`                                                                                                      |

`BIND_HOST`/`PORT` and `SERVICE_NAME`/`PORT` are deliberately separate concerns:
the previous implementation bound the hostname `router:50051` rather than an
interface, which works only where that name happens to resolve locally.

## Not persisted

Cluster state is in memory. A restart loses sessions - their warm containers are
then reclaimed by the workers' own TTLs - and workers re-register on their next
status report. That is an accepted trade, not an oversight: the alternative puts
a datastore on the hot path of every placement decision.
