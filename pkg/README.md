# readie

`readie` is the Python client for Readie, a service that runs Python functions
in gVisor sandboxes restored from checkpoints. Decorate a function with
`@remote`, and calls to it run on a Readie worker instead of in your process.

```sh
pip install readie
```

The client requires Python 3.12. See [Python version](#python-version).

For guides, concepts, and the SDK reference, see the documentation at
[readie.org](https://readie.org).

## Quick example

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

The client serializes the decorated function with `cloudpickle`, and a Python
interpreter inside a gVisor sandbox runs it. The function must therefore be
picklable, and its imports must exist in the worker's image or be declared with
`packages` (see [Packages](#packages)).

## Python version

The client and the sandbox must run the same Python minor version, because a
pickled function is portable only within one minor version. The sandbox runs
Python 3.12, so `readie.Client()` raises `IncompatiblePythonError` unless the
local interpreter is Python 3.12.

## Authentication

If the service requires a bearer token, set the `READIE_AUTH_TOKEN` environment
variable before creating the client. If the variable is unset, the client sends
no token.

## Resource budgets

Declare how much memory a function needs on the decorator. Each budget takes a
byte count or a size string such as `"512Mi"` or `"4Gi"`. An unset budget falls
back to the cluster default.

```python
@remote(gpu=True, memory="2Gi", max_memory="8Gi")
def train(rows): ...
```

`memory` is the container's initial limit, and `max_memory` is the ceiling that
the worker can expand to when the function needs more. `gpu_memory` and
`max_gpu_memory` set the same limits for GPU device memory. `gpu=True` sends the
call to a GPU worker, and a `gpu_memory` budget implies it.

The client also extracts the function's imports statically and forwards them, so
that the router can pick a checkpoint that already imported them. This is
automatic and needs no configuration.

## Packages

Declare the PyPI requirements that a function needs beyond what the checkpoint's
root filesystem already contains. The executor installs them with `uv` before
every call:

```python
@remote(packages=["numpy==1.26.0", "requests"])
def fetch(url): ...
```

Entries are requirement strings (a bare name or a full spec like
`"numpy==1.26.0"`), normalized and deduped by distribution name at decoration
time - `"Requests"` and `"requests"` collapse to one entry. Installation is
unconditional: it runs on every call, even when the package looks already
present. Independent calls get fresh containers; session calls reuse their warm
interpreter, but installation is not cached.

Installation is unconditional. It runs on every call, even when the package
appears to be present, because each call restores a fresh, isolated container
and there is nothing to verify the claim against.

## Client options

The module-level client is created on first use. To change its options, call
`readie.configure` before the first remote call:

```python
import readie

readie.configure(timeout=120.0, chunk_size=64 * 1024, stream_logs=True)
```

| Option | Description |
| --- | --- |
| `timeout` | Deadline for a whole call, in seconds. The default is no deadline. |
| `chunk_size` | Payload bytes per stream message. The default is 1 MiB. |
| `stream_logs` | If true, print the function's output as it arrives instead of only on failure. The default is true. |

## When a remote function raises

If the remote function raises, the client raises `readie.RemoteExecutionError`
with the traceback from the process that raised:

```python
try:
    train()
except readie.RemoteExecutionError as error:
    print(error.remote_type)  # "ValueError"
    print(error.remote_traceback)  # the frames inside the sandbox
    print(error.worker_id, error.container_id)
```

The client does not reconstruct the exception object. Unpickling it would
require its class to be importable locally, and for a library that exists only in
the worker image, the resulting `ImportError` would replace the real error with a
misleading one.

`EmptyResultError` indicates a different failure: the worker returned nothing at
all, which means the executor died before it could report an outcome. A function
that returns `None` returns a value and does not raise `EmptyResultError`.

## Sessions

By default, every call is independent and gets a fresh container. To reuse a warm
container and the Python state it holds, open a session:

```python
with readie.default_client().session() as s:
    load_data.bind(s)()  # cold start
    train.bind(s)()  # resumes the same interpreter
```

The router serializes calls within one session. A session maps to one container
that runs one interpreter behind one socket, so its calls cannot run at the same
time. Unrelated sessions run in parallel.

### Shared globals

By default, sessions share one globals dictionary across their remotely called
**plain Python functions** (`Client(session_globals=True)`). Initialize persistent
state remotely using `global`:

```python
@remote
def initialize():
    global counter, items
    counter = 0
    items = []


@remote
def increment():
    global counter
    counter += 1
    items.append(counter)
    return counter


with readie.Session() as s:
    initialize.bind(s)()
    assert increment.bind(s)() == 1
    assert increment.bind(s)() == 2
```

To reuse a warm container without replacing the submitted callable's globals,
disable session globals for that client:

```python
with readie.Client(session_globals=False) as client, client.session() as s:
    client.call(train.func, session=s)
```

The option applies to both blocking and async calls, including remote functions
using that client. Session IDs, routing, serial execution, and container reuse
remain unchanged. Calls keep their ordinary cloudpickle globals/callable behavior
and do not require a session-globals acknowledgment or generate ignored-globals
warnings. Disabling does not clear previously initialized shared session state;
unscoped calls are unaffected regardless of the option. For default `@remote`
calls, install the opted-out client with
`readie.configure(client=readie.Client(session_globals=False))`.

**With session globals enabled, client globals are discarded on every session
call, even the first.** This
includes constants, imports, helpers, and a function's own name; nothing is
seeded from the client or automatically registered under the submitted function's
name. A missing name raises `NameError`. Import inside the function; use `global`
for an import or value that later functions should access. Unscoped calls retain
their existing globals behavior.

**Methods decorated with `@remote` are supported in sessions.** The SDK submits
the underlying Python function and passes `self` as an argument, so that function
uses the same session globals as other remote functions. The receiver's instance
attributes are not automatically persisted across calls.

With session globals enabled, submitting a raw bound method directly through
`Client.call(obj.method, session=s)` is not supported. Partials, callable objects,
and built-ins are also unsupported as session-call targets in that mode. Setting
`session_globals=False` retains support for those ordinary callable types.

Discarded bindings produce `readie.IgnoredGlobalsWarning`, even with
`stream_logs=False`. Standard module scaffolding does not trigger a warning.
Filter it using Python's warning controls:

```python
import warnings

warnings.filterwarnings("ignore", category=readie.IgnoredGlobalsWarning)
```

Warnings are emitted after execution, including on remote failures. Promoting one
to an error does not undo the remote call. Structured diagnostics also travel in
the result envelope's optional `warnings` field.

Rebindings, object mutations, and deletions persist; locals do not. Assignments
made before a function raises or its result fails to serialize remain in place.
Closures and defaults still use ordinary per-call cloudpickle semantics: they are
not merged into the session namespace, and functions captured there retain their
own globals. Incoming globals are serialized/unpickled before being discarded,
so they can still cause serialization failures.

State is **in memory only**, for the warm sandbox's lifetime. Restarting or losing
the sandbox loses its state; this does not isolate imported-module caches or
provide background-thread synchronization or durable storage.

### Compatibility and deployment

Custom `ResultCodec.encode_call` implementations used with session globals enabled
must accept keyword-only `session_globals: bool = False` and serialize the optional
`session_globals: True` request field. Unscoped and opted-out calls keep the old
invocation. Successful responses with session globals enabled must acknowledge
`session_globals_applied: True`;
otherwise the SDK raises `ConfigurationError`. That call may already have run,
so do not retry it automatically. Older clients retain their original behavior.

Deploy the SDK with the matching executor. Rebuild the baked executor rootfs and
regenerate checkpoints/base images with the repository's generation workflow;
rebuilding only the Go worker does not update the executor. Existing warm
sessions are not migrated: reopen them on the updated deployment.

Calls inside one session are **serialised by the router**: a session maps to one
container running one interpreter behind one socket, so they cannot safely run at
once. Unrelated sessions stay fully parallel.

`client.session()` is a convenience wrapper. `Session` is a plain class that you
can construct directly, so the context-manager form is optional:

```python
s = readie.Session()
load_data.bind(s)()
train.bind(s)()
# ... later, possibly in another function
s.close()
```

A call can run inside a session in three equivalent ways: at decoration time,
through a bound copy, or per call.

```python
@remote(session=s)  # every call through this name uses s
def f(): ...


g = train.bind(s)  # a copy of an existing @remote bound to s; the original
# is module-level and shared, so it is left unchanged

readie.default_client().call(train.func, session=s)  # one call, through the client directly
```

`Session` also accepts an explicit id, for example `Session(session_id="sess-...")`.
This lets you reconstruct a session in another process, or after the current
process restarts, as long as the session's container still exists.

### Closing and expiry

Closing a session is a local operation. It stops this client from using the id,
and no message reaches the router. The router never removes a session on a timer
or a cap of its own. It retires a session id only after the container behind the
session is gone, typically because the worker removed the container after its
idle timeout.

A session can therefore outlive `close()`. The following two cases differ:

- A `session_id` that the router has never seen, including the id of a new
  `Session()`, is not an error. The call cold-starts, exactly like a call with no
  session.
- A `session_id` whose container the router has already reclaimed raises
  `readie.SessionExpiredError`. The router raises the error instead of
  cold-starting under an id that appears to carry warm state. Open a new
  `Session` instead of retrying the expired one.
