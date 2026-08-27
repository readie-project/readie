# crfs-executor

The program that runs inside a checkpoint-restore sandbox. It pre-imports a
planned set of modules, announces that it is worth checkpointing, and then
serves execution requests from the worker over a unix socket: unpickle a call,
make it, pickle the result back.

```sh
make install   # sync the virtualenv from the lockfile
make test      # framing and server tests over socketpair()
make lint type # ruff + mypy --strict
make help      # list every target
```

## Startup order

The order is the design, not a detail:

```
capture mode only:
  preimport CRFS_PREIMPORT   →  the reason a checkpoint is worth taking
  print READY_FOR_CHECKPOINT →  the pipeline greps for exactly this
  sleep CRFS_CHECKPOINT_SLEEP →  the capture window

bind $EXECUTOR_DIR/executor.sock
accept, serve, repeat
```

An ordinary worker does not enable capture mode, so its terminal stream contains
only output from the called function and errors.

Binding happens *after* the sleep on purpose. A socket bound earlier would have
its inode captured in the checkpoint image, and a restored sandbox would come
back holding a socket attached to a worker that no longer exists.

The consequence, which surprises people: a restored sandbox resumes part-way
through that sleep and finishes the remainder before it binds. That is why the
worker's `DIAL_TOTAL_TIMEOUT` has to exceed `CRFS_CHECKPOINT_SLEEP`.

## The wire protocol

Version 2. The worker dials; the executor is the server.

```
message:   ([8-byte big-endian length][chunk])* [8 zero bytes]

request:   one message, the cloudpickle of {func, args, kwargs}
response:  one message, the cloudpickle of a result envelope

envelope = {"ok": True,  "value": <return value>}
         | {"ok": False, "exc_type": str, "message": str, "traceback": str}
```

Framed per chunk rather than once per message so a sender can stream without
knowing the total size. The worker relays request bytes straight from a gRPC
stream and learns the length only when that stream ends; a single prefix would
force it to buffer an entire payload — possibly hundreds of megabytes — purely
to count it.

The Go counterpart is `worker/internal/executor`. The two are separate
implementations of one format, and `tests/data/frames.golden.json` is generated
here and decoded there, so they cannot drift apart silently. Regenerate it with
`uv run python tests/generate_golden.py`.

`protocol.py` is pure — it operates on a two-method `Reader`/`Writer` seam and
opens nothing — so every framing case is a unit test with no socket: a frame
delivered one byte at a time, a stream that ends mid-body, a length prefix large
enough to exhaust memory.

### What version 1 could not express

The previous format ended a request with the unframed literal bytes `EOF` and a
response by closing the connection.

- **A body could not contain its own terminator.** Pickle always ends with the
  STOP opcode, so this never fired — luck, not design.
- **A truncated response was indistinguishable from a short one.** An executor
  killed mid-write was reported as a success.
- **A raising function sent nothing at all.** Every remote exception surfaced as
  an empty response, and the traceback had to be scraped out of captured stderr.

`ok: False` is deliberately *not* a worker failure. The sandbox ran, the
interpreter is healthy, and the container is still reusable — so the worker
reports success and pauses it for reuse. Only the client turns the envelope into
a `RemoteExecutionError`, carrying the traceback from the process that raised.

A generation records `executor_protocol` in its manifest, and a worker refuses
one whose version it does not implement rather than dialing an executor that
will never send a terminator it recognises.

## Configuration

| | |
|---|---|
| `EXECUTOR_DIR` | **Required.** Directory to bind the socket in; set by the sandbox spec |
| `CRFS_CAPTURE_MODE` | Enables the offline pre-import, ready signal, and capture window; unset in ordinary workers |
| `CRFS_PREIMPORT` | Comma-separated modules to import before the checkpoint |
| `CRFS_CHECKPOINT_SLEEP` | Capture window in seconds; `0` skips it |
| `EXECUTOR_CHUNK_SIZE` | Read/write granularity, default 1 MiB |
| `EXECUTOR_SOCKET_NAME` | Default `executor.sock` |

A failed pre-import is reported and skipped rather than fatal. One bad name in a
planned set should leave a checkpoint that is missing a module, not a sandbox
that exited — and the pipeline can only tell those apart if this keeps going and
says so.

## Trust

This process runs arbitrary code by design; that is its entire job. Containment
is gVisor, no network, and a read-only shared rootfs with a per-sandbox overlay.
See [SECURITY.md](../SECURITY.md).
