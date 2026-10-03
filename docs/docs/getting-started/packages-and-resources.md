---
title: Packages and resources
sidebar_position: 3
description: Install extra PyPI packages per call, set memory and GPU budgets, and identify which objects can be pickled.
---

A function runs on a remote worker, so its requirements are declared as keyword arguments to `@remote`: the extra Python packages to install, the starting memory, and whether a GPU is needed. This page describes each keyword and the limits on what a call can carry.

## Packages

The sandbox provides a prepared Python environment. To use a library that the environment does not contain, list it in `packages`:

```python
from readie import remote


@remote(packages=["requests", "numpy==1.26.0"])
def fetch(url):
    import requests
    return requests.get(url).status_code
```

Each entry is a standard pip requirement: a bare name, a pinned version, a range, or a name with extras such as `"requests[security]>=2.31"`.

### Package installation

- Before the function runs, the executor inside the sandbox runs `uv pip install` with the list.
- The install runs on every call. Each call gets a fresh sandbox, so nothing is cached between calls, and large or slow packages add to the latency of every call.
- Unpinned names install the newest version available at that moment. Pin versions (`numpy==1.26.0`) for repeatable results.
- The sandbox needs network access to reach the package index.
- If the install fails, the call fails with `RemoteExecutionError`. The installer output is in the error message and in `err.logs`.
- Packages that are already in the checkpoint's environment are installed again, because Readie does not check for them.

At decoration time, the client validates and normalizes the list. Names are compared as PyPI compares them, so `Requests` and `requests`, or `scikit_learn` and `scikit-learn`, count as one entry, and the first spelling is kept. The list is sorted. A malformed entry raises `InvalidPackageError` at decoration time, before anything is sent.

## Memory and GPU budgets

```python
@remote(memory="2Gi", max_memory="8Gi")
def crunch(rows): ...

@remote(gpu=True, gpu_memory="16Gi")
def train(data): ...
```

| Keyword | Meaning |
| --- | --- |
| `memory` | Memory the sandbox starts with. |
| `max_memory` | Ceiling the worker may grow the memory to. |
| `gpu` | Send the call to a GPU worker. |
| `gpu_memory` | GPU memory to reserve. Implies `gpu=True`. |
| `max_gpu_memory` | Ceiling for GPU memory. |

If neither a starting value nor a ceiling is set for a resource, the cluster default applies. The router default memory budget is 1 GiB, and the default container limit of a worker is also 1 GiB. The operator of the cluster can change both values.

A ceiling lower than its starting value raises `ConfigurationError` at decoration time.

### Units

A size is an integer number of bytes or a string with a suffix:

| Suffix | Meaning | Example |
| --- | --- | --- |
| `Ki`, `Mi`, `Gi`, `Ti` (also `KiB`, `MiB`, ...) | Powers of 1024. | `"512Mi"` is 536,870,912 bytes. |
| `K`, `M`, `G`, `T` (also `KB`, `MB`, ...) | Powers of 1000. | `"2G"` is 2,000,000,000 bytes. |
| none | Bytes. | `1073741824` |

Sizes must be whole numbers. The client rejects `"1.5Gi"`, negative values, and `True` or `False` with `ConfigurationError`.

### Memory exhaustion

The worker uses two mechanisms to avoid out-of-memory kills:

1. **Early growth.** The worker watches the sandbox. When usage passes 90% of the limit, the worker doubles the limit. Both numbers are worker defaults that the operator can change. Growth stops at `max_memory`, or, if none is set, at a share of the total memory of the worker.
2. **One retry.** If a call ends without sending any complete response, which is how a memory kill appears, the worker retries the call once in a fresh sandbox with a larger limit, if there is room to grow. The worker cannot observe the real cause, so the retry is a heuristic.

Two consequences follow:

- A retried function runs twice. If the function has side effects, such as writing to a database, make them safe to repeat or set a high `memory` value up front.
- Calls inside a [session](/docs/concepts/sessions) are never retried this way, because a new sandbox would lose the state of the session.

For GPU memory, the router uses the budget when it chooses a worker. This page does not cover growth of GPU memory, because the behavior could not be verified in the code.

## Pickling limits

The function, its arguments, and its return value cross the network as pickles created by [cloudpickle](/docs/concepts/glossary). Most Python objects serialize, with the following limits.

Objects that serialize include plain functions and lambdas, classes, closures over ordinary values, NumPy arrays, pandas frames, and dataclasses.

Objects tied to a live resource on the local machine do not serialize. Examples are open files, sockets, database connections, locks, and threads. Encoding such an object fails with `SerializationError` before anything is sent. Create these objects inside the function instead.

Further constraints:

- **Imports run in the sandbox.** A module that the function imports must be installed there, either in the worker image or through `packages=`. The client scans the function for imports and forwards them as hints, so that the router can choose a snapshot that already contains them. This happens automatically.
- **Local modules are not copied.** cloudpickle sends functions defined in a script or notebook by value, but references functions from an importable module by name. The sandbox has no copy of the local project, so a function that calls into a local package fails with an import error. Put the code in the decorated function, or publish the package and list it in `packages=`.
- **Return values are unpickled locally.** The classes involved must be importable on the local machine.
- **Python versions must match.** See [Install the client](/docs/getting-started/installation).

## What's next

- [Sessions and errors](/docs/getting-started/sessions-and-errors): keep state between calls and handle exceptions.
- [Tune your calls](/docs/guides/configure): memory, timeouts, and output settings.
