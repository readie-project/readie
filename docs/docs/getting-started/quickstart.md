---
title: Quickstart
sidebar_position: 2
description: Run a function remotely with the @remote decorator, in blocking and async form.
---

The `@remote` decorator marks a function to run in a sandbox on a Readie worker. The client packs the function, sends it to a worker, and returns the result as if the function had run locally.

## Prerequisites

- Python 3.12 and the `readie` package. See [Install the client](/docs/getting-started/installation).

## Step 1: Run a remote function

Decorate a function with `@remote` and call it:

```python
from readie import remote


@remote
def add(a, b):
    return a + b


print(add(1, 2))
```

Example output:

```text
Python version: 3.12.x (main, ...)
cloudpickle version: 3.1.x
Execution completed in 0.4203518s
3
```

The SDK prints the first two lines and the `Execution completed in ...` line on every call. They report the local interpreter version, the cloudpickle version, and the wall-clock time of the round trip. The timing value is illustrative and depends on the cluster.

Decorating a function does no work and opens no connection. The first call connects.

## Step 2: Call from async code

A running event loop refuses blocking calls, because they would freeze the loop. In async code, call the `.aio` variant and await it:

```python
import asyncio
from readie import remote


@remote
def add(a, b):
    return a + b


async def main():
    results = await asyncio.gather(add.aio(1, 2), add.aio(3, 4))
    print(results)


asyncio.run(main())
```

Calling `add(1, 2)` from inside `main()` raises `BlockingCallInEventLoopError`.

## Step 3: Handle imports and printed output

Imports must exist in the sandbox, not on the local machine. A function that needs `pandas` works if the worker image contains `pandas` or if the call requests it with `packages=`. See [Packages and resources](/docs/getting-started/packages-and-resources).

The sandbox captures anything the function prints. The client writes it to stderr after the call finishes, unless `stream_logs=False` is set. Output does not appear while the function runs.

```python
@remote
def greet(name):
    print("hello from the sandbox")
    return f"hi {name}"
```

## Step 4: Handle exceptions

An exception raised inside the function returns as `RemoteExecutionError`, which carries the traceback from the sandbox:

```python
import readie


@remote
def fail():
    raise ValueError("bad input")


try:
    fail()
except readie.RemoteExecutionError as err:
    print(err.remote_type)     # ValueError
    print(err.remote_message)  # bad input
```

The client does not recreate the original exception object. It provides the type name, message, and traceback as text.

## Step 5: Test the function body locally

The `.local(...)` method runs the function in the current process without a remote call. Use it in unit tests:

```python
assert add.local(1, 2) == 3
```

## Request path

A remote call proceeds in four stages:

1. The client pickles the function, the arguments, and the package list.
2. The client streams the payload to the router, which chooses a worker.
3. The worker starts a sandbox, usually by restoring a snapshot that has already imported common libraries.
4. The function runs, and the result streams back.

For the full sequence, see [Request lifecycle](/docs/architecture/request-lifecycle). For the reason restoring is faster than a cold start, see [Cold starts and restore](/docs/concepts/cold-starts-and-restore).

## What's next

- [Packages and resources](/docs/getting-started/packages-and-resources): extra libraries, memory, and GPUs.
- [Sessions and errors](/docs/getting-started/sessions-and-errors): keep state between calls and handle failures.
