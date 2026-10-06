---
title: Frequently asked questions
sidebar_position: 5
description: Short answers to common questions about using Readie, how it works, and its security.
---

## Using Readie

### What does Readie do?

Readie runs a Python function that is decorated with `@remote` on a remote worker, inside a sandbox. The worker restores a saved checkpoint of a Python process that has already imported common libraries, which skips most of the import time at the start of a call. See [Cold starts and restore](/docs/concepts/cold-starts-and-restore).

### Why does Readie require Python 3.12?

The function is pickled on the local machine and unpickled by the Python interpreter inside the sandbox. Pickled functions are safe to share only between the same minor Python version, and the sandbox runs Python 3.12. If the local interpreter is not Python 3.12, `readie.Client()` raises `IncompatiblePythonError`, so the failure appears at startup instead of later. See [Install the client](/docs/getting-started/installation).

### Which objects cannot be sent to a remote function?

Any object that `cloudpickle` cannot serialize. Typical cases are open files, sockets, database connections, locks, and threads. Create these objects inside the function instead of passing them in. The return value must also be picklable, because it travels back the same way. If the function or its arguments cannot be encoded, the client raises `SerializationError`. See the [errors reference](/docs/guides/errors).

### How do I use a package that is not in the sandbox?

List the package in `@remote(packages=["numpy==1.26.0", "requests"])`. The executor installs the packages with `uv` before every call, and nothing is cached between calls, so each call pays the install time. Packages already in the sandbox image do not need to be listed. See [Packages and resources](/docs/getting-started/packages-and-resources).

### Does state persist between calls?

No, not by default. Each call is independent and gets a fresh container. To keep state, such as a loaded model, open a session and make the calls under it. Calls in one session go to the same warm container and run one at a time. A warm container that stays idle is destroyed after the idle TTL, which is 5 minutes by default. See [Sessions](/docs/concepts/sessions) and [Sessions and errors](/docs/getting-started/sessions-and-errors).

### Is a GPU required?

No. GPU use is opt-in. With `@remote(gpu=True)` or a `gpu_memory` budget, the router sends the call to a GPU worker. Without them, the call prefers a CPU worker. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

### How do I set memory limits?

Pass `memory` and `max_memory` to `@remote`, for example `@remote(memory="2Gi", max_memory="8Gi")`. The value `memory` is the starting limit. If the container approaches that limit, the worker raises it, up to `max_memory`. See [Packages and resources](/docs/getting-started/packages-and-resources).

### How do I turn off checkpoint restore for one function?

Use `@remote(disable_optimized_execution=True)`. The router then starts that function cold. See the [SDK reference](/docs/guides/sdk).

## How it works

### What does a restore cost?

A restore is cheaper than importing the libraries again, but it has a cost. It copies the saved process memory into a new sandbox, so larger checkpoints cost more, and the router weighs the size of a checkpoint against the import time that it saves. The exact numbers depend on the hardware and the checkpoint. See [Cost model](/docs/concepts/cost-model).

### What happens if no checkpoint fits my function?

The call runs as a normal cold start. The function still works but is not faster. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

### Does the client work on macOS or Windows?

Yes, if the interpreter is Python 3.12. The function runs remotely in a Linux sandbox, so the operating system and CPU of the local machine do not matter. See [Install the client](/docs/getting-started/installation).

## Security

### Is Readie secure?

User code runs inside a [gVisor](/docs/concepts/glossary) sandbox with a shared read-only filesystem, a private writable layer, and limits on CPU, memory, and processes. The client unpickles the result that it receives, so connect only to a Readie service that you trust. See [Security](/docs/architecture/security) and [Security model](/docs/architecture/security-model).

## Getting help

### Where do I report a bug or ask a question?

Open an issue on [GitHub](https://github.com/illinoisdata/readie/issues/new/choose) and use the bug report template. For anything exploitable, use the private route described in [Security](/docs/architecture/security).
