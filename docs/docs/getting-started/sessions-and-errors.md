---
title: Sessions and errors
sidebar_position: 4
description: Keep Python state between calls with sessions, and handle each exception that the SDK raises.
---

By default, each call runs in a new sandbox and retains no state. A session routes several calls to one warm sandbox, so a loaded model or imported library stays in memory. The SDK reports failures as exceptions that all inherit from `ReadieError`.

## Sessions

A `Session` is a label. Calls made with the same label go to the same sandbox.

```python
import readie
from readie import remote


@remote
def load():
    global data
    data = list(range(1_000_000))
    return len(data)


@remote
def total():
    return sum(data)


client = readie.default_client()
with client.session() as s:
    load.bind(s)()   # runs first
    total.bind(s)()  # same sandbox: `data` is still there
```

The `bind` method returns a copy of the remote function that is tied to the session. The original function is unchanged. Alternatively, set the session at decoration time with `@remote(session=s)`, or pass `session=` to `client.call(...)`.

Sessions follow these rules:

- **One call at a time.** A sandbox has one Python interpreter, so the router queues calls for a session. Use separate sessions for parallel work.
- **Closing is local.** Calling `close()` or leaving the `with` block only stops the client from using the session id. It sends nothing. The sandbox remains until the worker removes it after it has been idle (5 minutes by default).
- **Sessions expire.** After the sandbox is removed, reusing the id raises `SessionExpiredError`. Start a new `Session`.
- **Unknown ids are accepted.** If the router has never seen an id, the call starts a new sandbox under that id.

For the internals, see [Sessions](/docs/concepts/sessions).

## Exceptions

Every SDK exception is a `ReadieError`, so `except readie.ReadieError` catches all of them. For the complete table, see the [errors reference](/docs/reference/errors). The following sections cover the most common exceptions.

### Function failures

`RemoteExecutionError` indicates that the function raised an exception. The exception provides `remote_type`, `remote_message`, and `remote_traceback`, and the traceback is also in the exception text. The attributes `logs`, `worker_id`, and `container_id` help when an operator investigates the failure.

```python
try:
    job()
except readie.RemoteExecutionError as err:
    print(err.remote_type, err.remote_message)
```

A failed `packages=` install also raises this exception. Fix the function or the package list.

`EmptyResultError` indicates that the executor in the sandbox ended without reporting anything, for example because the process was killed. A function that returns `None` does not cause this error. A retry might succeed. High memory use is a common cause, so consider raising `memory`.

### Calls that did not run

| Exception | Usual cause | Action |
| --- | --- | --- |
| `ClusterUnavailableError` | The service is unreachable, or no workers are available. | Retry later. |
| `PermissionDeniedError` | `READIE_AUTH_TOKEN` is missing or wrong. | Set the token. |
| `RemoteTimeoutError` | The `timeout` elapsed. | Raise the timeout, or leave it unset. |
| `ResourceExhaustedError` | The cluster is full, or too many calls are queued on one session. | Retry later, or spread work over sessions. |
| `SessionExpiredError` | The sandbox of the session was removed. | Open a new session. |
| `InvalidRequestError` | The request was rejected, including `disable_optimized_execution=True` on a session that already runs from a snapshot. | Fix the request. |

### Code and setup errors

| Exception | Cause |
| --- | --- |
| `IncompatiblePythonError` | The local interpreter is not Python 3.12. |
| `ConfigurationError` | Invalid settings or budgets, such as a negative size or a ceiling below the starting value. |
| `InvalidPackageError` | A `packages=` entry is malformed. |
| `SerializationError` | A value cannot be pickled, or the result cannot be unpickled. |
| `BlockingCallInEventLoopError` | A blocking call ran inside async code. Use `await fn.aio(...)`. |
| `ClientClosedError` | The code used a closed client or session. |

Transport errors carry a `code` attribute with the gRPC status name, such as `"UNAVAILABLE"`.

## Cancellation

If a call is interrupted with Ctrl-C, or the asyncio task that awaits it is cancelled, the client cancels the request on the router so that it does not continue to run.

## What's next

- [Troubleshooting](/docs/guides/troubleshooting): match an error message to its cause and fix.
- [Errors reference](/docs/reference/errors): every exception class.
- [Sessions](/docs/concepts/sessions): how sessions work inside the cluster.
