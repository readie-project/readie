# crfs-client

Run a Python function on a remote checkpoint-restore worker by decorating it.

```python
from crfs import remote


@remote
def add(a, b):
    return a + b


print(add(1, 2))  # blocking
print(await add.aio(1, 2))  # from an event loop
```

The decorated function is serialised with `cloudpickle` and executed by a Python
interpreter inside a gVisor sandbox, so it must be picklable and its imports must
exist in the worker's image.

## Resource budgets

Declare how much memory a function needs on the decorator. Each budget takes a
byte count or a size string (`"512Mi"`, `"4Gi"`); an unset budget falls back to
the cluster default.

```python
@remote(gpu=True, memory="2Gi", max_memory="8Gi")
def train(rows): ...
```

`memory` is the container's initial limit and `max_memory` the ceiling the
worker may auto-expand to when the function needs more; `gpu_memory` /
`max_gpu_memory` are the same for GPU device memory. `gpu=True` sends the call to
a GPU worker (a `gpu_memory` budget implies it too). The client also statically
extracts the function's imports and forwards them so the router can pick a
checkpoint that already imported them — this is automatic and needs no
configuration.

```sh
make install   # sync the virtualenv from the lockfile
make test      # unit + in-process gRPC integration tests
make lint type # ruff + mypy --strict
```

Configure explicitly, which takes precedence over the environment:

```python
import crfs

crfs.configure(router_uri="router:50051", timeout=120.0)
```

Or from the environment, read by `Settings.from_env` when a client is built
without arguments:

| | |
|---|---|
| `CRFS_ROUTER_URI` | `host:port` of the router (default `localhost:50051`). `ROUTER_URI` is accepted as a fallback |
| `CRFS_TIMEOUT` | deadline for a whole call, in seconds; unset means no deadline |
| `CRFS_CHUNK_SIZE` | payload bytes per stream message (default 1 MiB) |
| `CRFS_MAX_MESSAGE_BYTES` | gRPC message cap (default 16 MiB); must exceed `CRFS_CHUNK_SIZE` |
| `CRFS_STREAM_LOGS` | print executor output as it arrives, not only on failure (default on) |
| `CRFS_AUTH_TOKEN` | bearer token for a router that requires one; unset sends none |
| `CRFS_TLS`, `CRFS_TLS_CA` | connect over TLS; a CA path verifies the router, else system roots |

## When a remote function raises

You get the real traceback, from the process that raised it:

```python
try:
    train()
except crfs.RemoteExecutionError as error:
    print(error.remote_type)  # "ValueError"
    print(error.remote_traceback)  # the frames inside the sandbox
    print(error.worker_id, error.container_id)
```

The exception *object* is not reconstructed. Unpickling it would need its class
importable here, and for a library that exists only in the worker image the
resulting `ImportError` would replace the real error with a confusing one.

`EmptyResultError` means something else entirely: the worker returned nothing at
all, so the executor died before it could report an outcome. A function that
returns `None` returns a value and does not land there.

## Sessions

By default every call is independent and gets a fresh container. To reuse a warm
container — and the Python state it holds — open a session:

```python
with crfs.default_client().session() as s:
    load_data.bind(s)()  # cold start
    train.bind(s)()  # resumes the same interpreter
```

Calls inside one session are **serialised by the router**: a session maps to one
container running one interpreter behind one socket, so they cannot safely run at
once. Unrelated sessions stay fully parallel.
