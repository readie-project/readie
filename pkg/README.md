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

```sh
make install   # sync the virtualenv from the lockfile
make test      # unit + in-process gRPC integration tests
make lint type # ruff + mypy --strict
```

Configure with `CRFS_ROUTER_URI` (default `localhost:50051`), or explicitly:

```python
import crfs

crfs.configure(router_uri="router:50051", timeout=120.0)
```

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
