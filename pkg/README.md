# readie-client

Run a Python function on a remote checkpoint-restore worker by decorating it.

```python
from readie import remote
import asyncio


@remote
def add(a, b):
    return a + b


print(add(1, 2))  # blocking


async def add_async(a, b):
    return await add.aio(a, b)


print(asyncio.run(add_async(1, 2)))  # from an event loop
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
checkpoint that already imported them - this is automatic and needs no
configuration.

## Packages

Declare PyPI requirements a function needs beyond what the checkpoint's rootfs
already carries. The executor installs them with `uv` before every call:

```python
@remote(packages=["numpy==1.26.0", "requests"])
def fetch(url): ...
```

Entries are requirement strings (a bare name or a full spec like
`"numpy==1.26.0"`), normalized and deduped by distribution name at decoration
time - `"Requests"` and `"requests"` collapse to one entry. Installation is
unconditional: it runs on every call, even when the package looks already
present, since each call restores a fresh, isolated container with nothing to
check that claim against.

```sh
make install   # sync the virtualenv from the lockfile
make test      # unit + in-process gRPC integration tests
make lint type # ruff + mypy --strict
```

Configuration options:

```python
import readie

readie.configure(
    router_uri="localhost:50051", timeout=120.0, chunk_size=64 * 1024, stream_logs=True, tls=False
)
```

The following are configurable only from the environment:

|                                                          |                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------- |
| `READIE_AUTH_TOKEN`                                      | bearer token for a router that requires one; unset sends none |
| `READIE_TLS_CA`                                          | connect over TLS; a CA path verifies the nginx server, else system roots. TLS is required for HTTPS router URIs |

## When a remote function raises

You get the real traceback, from the process that raised it:

```python
try:
    train()
except readie.RemoteExecutionError as error:
    print(error.remote_type)  # "ValueError"
    print(error.remote_traceback)  # the frames inside the sandbox
    print(error.worker_id, error.container_id)
```

The exception _object_ is not reconstructed. Unpickling it would need its class
importable here, and for a library that exists only in the worker image the
resulting `ImportError` would replace the real error with a confusing one.

`EmptyResultError` means something else entirely: the worker returned nothing at
all, so the executor died before it could report an outcome. A function that
returns `None` returns a value and does not land there.

## Sessions

By default every call is independent and gets a fresh container. To reuse a warm
container - and the Python state it holds - open a session:

```python
with readie.default_client().session() as s:
    load_data.bind(s)()  # cold start
    train.bind(s)()  # resumes the same interpreter
```

Calls inside one session are **serialised by the router**: a session maps to one
container running one interpreter behind one socket, so they cannot safely run at
once. Unrelated sessions stay fully parallel.

`client.session()` is a convenience wrapper - `Session` itself is a plain,
constructible class, so you are not limited to the context-manager form:

```python
s = readie.Session()
load_data.bind(s)()
train.bind(s)()
# ... later, maybe in another function ...
s.close()
```

There are three equivalent ways to run a call inside a session - decoration
time, a bound copy, or per-call:

```python
@remote(session=s)  # every call through this name uses s
def f(): ...


g = train.bind(s)  # a copy of an existing @remote bound to s
# (the original is untouched, since it is
# module-level and shared)

readie.default_client().call(train.func, session=s)  # one-off, via the client directly
```

`Session()` also accepts an explicit id (`Session(session_id="sess-...")`), so
a session can be reconstructed - in another process, or after this one
restarts - as long as its container hasn't since been torn down (below).

**Closing a session is purely local.** It just stops this client using the
id; no message reaches the router. The router, in turn, never removes a
session on a timer or a cap of its own - only once the container behind it
is actually gone (typically the *worker's* own pause TTL reclaiming it; see
the worker README) does it retire the id. So a session can safely outlive
`close()`, but the two cases that follow are not the same:

- A `session_id` the router has genuinely never seen (including a brand-new
  `Session()`) is not an error - the call just cold-starts, exactly like a
  call with no session at all.
- Reusing a `session_id` whose container the router has already reclaimed
  raises `readie.SessionExpiredError` instead of silently cold-starting under
  an id that looks like it should still carry warm state. Open a new
  `Session` rather than retrying the old one.
