# readie-executor

The executor is the program that runs inside every checkpoint-restore sandbox.
In capture mode, it pre-imports a planned set of modules and triggers the gVisor
checkpoint. It then serves execution requests from the worker over a unix socket:
it unpickles a call, runs it, and pickles the result back.

For the protocol specification on the documentation site, see
[Executor wire protocol](https://readie.org/docs/architecture/executor-protocol).
For the procedure to change the protocol, see
[Changing the executor protocol](https://readie.org/docs/contributing/changing-executor-protocol).

```sh
make install   # sync the virtualenv from the lockfile
make test      # framing and server tests over socketpair()
make lint type # ruff + mypy --strict
make help      # list every target
```

Session-global unit tests cover namespace behavior and function reconstruction.
Socket integration tests cover serialization, persistence across requests,
executor isolation, and diagnostics on success and failure. They use the real
codec and socket protocol, without routing or gVisor.

## Startup order

The order of startup is part of the design. A checkpoint freezes the process
part-way through startup, so each step must run before or after that point for a
specific reason.

```
capture mode only:
  preimport READIE_PREIMPORT       →  the reason a checkpoint is worth taking
  write to /proc/gvisor/checkpoint →  trigger gVisor checkpoint internally

after a restore:
  re-read the environment given to the new sandbox

measure mode only: exit

bind $EXECUTOR_DIR/executor.sock
accept, serve, repeat
```

The executor binds the socket after the checkpoint. A socket bound earlier would
be part of the checkpoint image, and a restored sandbox would hold a socket
attached to a worker that no longer exists.

An ordinary worker does not enable capture mode, so its terminal stream contains
only output from the called function and errors. If `EXECUTOR_DIR` is unset, the
executor exits with code 2.

## Wire protocol

The protocol is at version 2. The worker dials, and the executor is the server.

```
message:   ([8-byte big-endian length][chunk])* [8 zero bytes]

request:   one message, the cloudpickle of {func, args, kwargs, packages}
response:  one message, the cloudpickle of a result envelope

envelope = {"ok": True,  "value": <return value>, "output": [str, ...]}
         | {"ok": False, "exc_type": str, "message": str, "traceback": str,
            "output": [str, ...]}
```

`packages` is optional and defaults to empty. It holds PyPI requirement specs
that the executor installs with `uv pip install` before it invokes `func`. The
specs travel with the call instead of through a separate channel, so a client
that never sends them still decodes correctly. `output` holds text captured from
the function's stdout and stderr, plus the package install log.

A sender splits a message into chunks so that it can stream without knowing the
total size. The worker relays request bytes directly from a gRPC stream and
learns the length only when that stream ends. A single length prefix would force
the worker to buffer the entire payload, which can be hundreds of megabytes, only
to count it. Receivers check the limits before allocating memory: a chunk is at
most 64 MiB and a message is at most 4 GiB.

The Go counterpart is `worker/internal/executor`. The two are separate
implementations of one format. The fixture `tests/data/frames.golden.json` is
generated here and decoded there, so the implementations cannot drift apart
silently. To regenerate the fixture, run:

```sh
uv run python tests/generate_golden.py
```

`protocol.py` is pure. It operates on a two-method `Reader`/`Writer` seam and
opens nothing, so every framing case is a unit test with no socket. The cases
include a frame delivered one byte at a time, a stream that ends mid-body, and a
length prefix large enough to exhaust memory.

### Failed calls

An envelope with `ok: False` is deliberately not a worker failure. The sandbox
ran, the interpreter is healthy, and the container remains reusable. The worker
therefore reports success and keeps the container for reuse. Only the client
turns the envelope into a `RemoteExecutionError`, which carries the traceback
from the process that raised.

If the envelope itself cannot be pickled, for example because the return value is
not picklable, the executor sends an `ok: False` envelope that describes the
problem.

### Version check

A generation records `executor_protocol` in its manifest. A worker refuses a
generation whose version it does not implement, instead of dialing an executor
that never sends a terminator the worker recognizes. A manifest without the field
counts as version 1.

Version 1 used a literal `EOF` and a connection close. It could not distinguish
a short message from a truncated one, and a function that raised sent nothing
back. Version 2 fixes both.

## Session globals

Requests may include optional `session_globals: true` (false when
absent). The SDK enables it by default for session calls;
`Client(session_globals=False)` opts out without changing session affinity or
container reuse. One `ExecutorServer` owns one namespace, initialized after
checkpoint restoration with built-ins and minimal module scaffolding. Existing router affinity and serial execution provide its
session lifetime; the socket protocol needs no session ID.

Flagged calls must target a plain Python function. The executor reconstructs it
against the shared dictionary because `__globals__` is read-only. Its original
globals are discarded on every call, never merged, and never mutated. Closures,
defaults, annotations, and function metadata are preserved. Unflagged requests
retain the original callable behavior.

Methods decorated with `@remote` also satisfy this requirement: the SDK submits
their underlying Python function with `self` in the arguments. Raw bound-method
objects submitted directly do not. Instance attributes are ordinary serialized
argument state, not automatically persisted session globals.

Initialize shared values inside remote functions with `global`. Reads, rebindings,
mutations, imports bound globally, and deletion persist until the sandbox is gone.
Locals do not. The function is not registered under its own name automatically;
missing constants/helpers/imports and recursive self-bindings raise `NameError`.
State is not transactional: user exceptions and result-serialization failures
leave earlier mutations intact. Captured functions are not recursively rebound,
and client globals still pass through cloudpickle before being discarded.

After successful binding, responses include `session_globals_applied: true`, on
both success and subsequent failures. Discarded bindings other than standard
module scaffolding generate optional structured diagnostics:

```python
{"warnings": [{"code": "ignored_globals", "message": "...", "names": ["counter"]}]}
```

Only names, never values, are reported. Diagnostics and captured output survive
package/DNS setup failures, user exceptions, and result-serialization fallback.
The SDK emits `IgnoredGlobalsWarning` independently of log streaming.

These are additive pickle payload fields, not changes to protocol-2 framing or
protobufs. Deploy with the matching SDK and regenerate the baked rootfs and
checkpoints; rebuilding the worker alone leaves the old executor in place.
Existing sessions cannot be migrated and must be reopened. This feature is not
durable persistence, module-cache isolation, or a security boundary.

## Configuration

| Variable | Description |
| --- | --- |
| `EXECUTOR_DIR` | Required. Directory in which to bind the socket. The sandbox spec sets it. |
| `READIE_PREIMPORT` | Comma-separated modules to import before the checkpoint. |
| `EXECUTOR_MODE` | `capture` enables the offline pre-import and the checkpoint trigger. `measure` restores and then exits without starting the server. Unset, `sandbox`, or any unrecognized value selects ordinary serving, which is the default for workers. |
| `EXECUTOR_CHUNK_SIZE` | Read/write granularity. Default 1 MiB. |
| `EXECUTOR_SOCKET_NAME` | Socket file name. Default `executor.sock`. |

The executor serves one connection at a time, because a sandbox contains one
interpreter and runs one call at a time.

In capture mode, a module that fails to import is reported on stderr and the
exception propagates, so the pre-import stops at the first failure and the
executor does not reach the checkpoint.

## Trust

The executor runs arbitrary code by design; that is its entire job. Containment
comes from gVisor and from a shared rootfs that is never written, because each
sandbox gets an in-memory overlay.

The worker's `SANDBOX_NETWORK` setting defaults to `sandbox`, which gives the
sandbox a routed network. Set it to `none` for network isolation. For the
deployment posture, see [SECURITY.md](../SECURITY.md).
