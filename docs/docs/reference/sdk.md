---
title: SDK reference
sidebar_position: 1
description: Signatures, parameters, and behavior of the readie decorator, Client, Session, Settings, and configure.
---

The `readie` package exposes the `@remote` decorator, the `Client` and `Session` classes, and the configuration helpers. All names on this page are importable from the top-level `readie` package. The package requires Python 3.12.

## `remote`

```python
remote(func=None, /, *, client=None, timeout=None, session=None, gpu=False,
       memory=None, max_memory=None, gpu_memory=None, max_gpu_memory=None,
       packages=None, disable_optimized_execution=False)
```

The decorator is used bare (`@remote`) or with keywords (`@remote(memory="2Gi")`). Decorating performs no work and opens no connection.

**Parameters**

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `client` | `Client` or `None` | `None` | Client to use. `None` selects the process-wide default client. |
| `timeout` | `float` or `None` | `None` | Deadline for one call, in seconds. `None` falls back to `Settings.timeout`. |
| `session` | `Session` or `None` | `None` | Session in which every call runs. |
| `gpu` | `bool` | `False` | Routes calls to a GPU worker. |
| `memory` | `str`, `int`, or `None` | `None` | Starting memory. |
| `max_memory` | `str`, `int`, or `None` | `None` | Memory ceiling. |
| `gpu_memory` | `str`, `int`, or `None` | `None` | GPU memory. Implies `gpu=True` at the router. |
| `max_gpu_memory` | `str`, `int`, or `None` | `None` | GPU memory ceiling. |
| `packages` | sequence of `str`, or `None` | `None` | PyPI requirements installed before every call. |
| `disable_optimized_execution` | `bool` | `False` | Always cold-starts and never restores a snapshot. |

Sizes are bytes (`int`) or strings such as `"512Mi"`, `"4Gi"`, and `"2G"`. For the unit table, see [Packages and resources](/docs/getting-started/packages-and-resources).

**Returns** A `RemoteFunction`.

**Raises**

| Exception | Condition |
| --- | --- |
| `ConfigurationError` | A size is invalid, or a ceiling is below its starting value. Raised at decoration time. |
| `InvalidPackageError` | A `packages` entry is not a valid requirement. Raised at decoration time. |
| `InvalidRequestError` | `disable_optimized_execution=True` is used with a session whose sandbox was itself restored from a snapshot. |

## `RemoteFunction`

`RemoteFunction` is the object that `@remote` returns. It keeps the name and docstring of the decorated function. Decorated methods work, and the instance is passed as the first argument.

| Member | Description |
| --- | --- |
| `fn(*args, **kwargs)` | Runs the function remotely and blocks. Raises `BlockingCallInEventLoopError` inside a running event loop. |
| `await fn.aio(*args, **kwargs)` | Runs the function remotely without blocking. |
| `fn.local(*args, **kwargs)` | Runs the function in the current process, with no remote call and no serialization. |
| `fn.bind(session=None, *, client=None, timeout=None)` | Returns a copy bound to a session, client, or timeout. Arguments left as `None` keep the current value. The original is unchanged. |
| `fn.func` | The undecorated function. |

When a call succeeds, both `fn(...)` and `fn.aio(...)` print `Execution completed in <seconds>s` to stdout. The call-encoding step also prints the local Python and cloudpickle versions to stdout.

## `Client`

```python
Client(settings=None, *, codec=None, transport=None, async_transport=None, log_sink=None)
```

Creating a client checks the Python version and raises `IncompatiblePythonError` on a mismatch. It opens no connection, because channels connect on first use.

**Parameters**

| Name | Description |
| --- | --- |
| `settings` | A `Settings` instance. Defaults to `Settings()`. |
| `codec` | Object with `encode_call` and `decode_result` methods. Defaults to cloudpickle. |
| `transport`, `async_transport` | Replacements for the gRPC transports, for example in tests. |
| `log_sink` | `Callable[[str], None]` that receives remote output. The default writes to stderr. |

**Methods**

| Member | Description |
| --- | --- |
| `call(func, args=(), kwargs=None, *, session=None, timeout=None, budgets=(), gpu=False, packages=(), disable_optimized_execution=False)` | Runs `func` and blocks. Takes already-built `Budget` objects, which `remote` builds from its keywords. |
| `await acall(...)` | Asynchronous form of `call` with the same parameters. |
| `session(session_id=None)` | Context manager that yields a `Session`. |
| `close()`, `await aclose()` | Release connections. Both are idempotent. After closing, calls raise `ClientClosedError`. |
| `client.settings` | The `Settings` in use. |

A `Client` is a context manager and supports both `with` and `async with`.

## `Session`

```python
Session(session_id=None)
```

A `Session` is a handle that routes calls to the same warm sandbox. Its `id` is `"sess-"` followed by 32 hex characters unless a value is passed. Calls in one session run one at a time. For concepts, see [Sessions](/docs/concepts/sessions).

| Member | Description |
| --- | --- |
| `id` | The session identifier. |
| `closed` | Whether the session is closed. Using a closed session raises `ClientClosedError`. |
| `close()` | Closes the session. Idempotent and local only. |

A `Session` is also a context manager.

## `configure`, `default_client`, and `reset`

| Function | Description |
| --- | --- |
| `configure(client=None, **settings)` | Installs the process-wide client, either the one passed or `Client(Settings(**settings))`. Passing both raises `TypeError`. A previous default client is closed. Returns the client. |
| `default_client()` | Returns the process-wide client and creates `Client()` on first use. |
| `reset()` | Closes and drops the default client. |

Only `Settings` fields that take part in `__init__` can be passed to `configure`: `timeout`, `chunk_size`, and `stream_logs`.

## `Settings`

`Settings` is an immutable dataclass. The environment variable is read when `Settings` is created.

| Field | Default | Environment variable | Description |
| --- | --- | --- | --- |
| `timeout` | `None` | none | Seconds for a whole call. `None` sets no deadline. Must be positive. |
| `chunk_size` | `1048576` | none | Bytes per streamed message. Must be positive. |
| `stream_logs` | `True` | none | Prints the function's captured output when the call finishes. Must be a `bool`. |
| `auth_token` | `""` | `READIE_AUTH_TOKEN` | Bearer token. An empty value sends none. Not a constructor argument. |
| `max_message_bytes` | 16 MiB | none | gRPC message cap. Fixed and not a constructor argument. |

**Raises** `ConfigurationError` for an invalid value.

## `Budget` and `ResourceKind`

`Budget(kind, alloc=0, max=0)` is a byte-valued budget. `ResourceKind` defines `MEMORY = 1` and `GPU_MEMORY = 2`. A value of 0 selects the default.

## Results and exceptions

- A successful call returns the function's return value, unpickled on the local machine. `None` is a valid value.
- If the function raises, the call raises `RemoteExecutionError` locally, with the type, message, and traceback as strings. The original exception object is not rebuilt.
- Every other failure is a `ReadieError` subclass. See [Errors](/docs/reference/errors).
- With `stream_logs=True`, text that the function printed is sent to the log sink after the call completes. Output produced before a failure is attached to the error as `logs`.

## See also

- [Errors](/docs/reference/errors)
- [Sessions](/docs/concepts/sessions)
- [Packages and resources](/docs/getting-started/packages-and-resources)
