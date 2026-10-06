---
title: Install the client
sidebar_position: 1
description: Install the readie Python package and verify that it imports on Python 3.12.
---

The Readie client is a Python package that sends functions decorated with `@remote` to the hosted Readie service, which runs them and returns the result. Install the client on the machine that runs your program.

## Prerequisites

- Python 3.12. The client requires this minor version.
- A package installer: `pip` or `uv`.

The client requires Python 3.12 because the function is serialized locally with [cloudpickle](/docs/concepts/glossary) and unpickled by the Python interpreter inside the sandbox, which runs Python 3.12. Pickled bytecode is portable only within one minor version of Python.

The client checks the interpreter version when it creates a `Client`, which `@remote` does on first use. On any other minor version, the check fails with the following error:

```text
readie.errors.IncompatiblePythonError: Readie requires Python 3.12, found Python 3.13
```

The package metadata also declares `requires-python = "~=3.12.0"`, so `pip` refuses to install the package on other versions.

## Steps

1. Install the package from [PyPI](https://pypi.org/project/readie). The package is named `readie` and is imported as `readie`.

   ```bash
   pip install readie
   ```

   Alternatively, add it to a project with `uv`:

   ```bash
   uv add readie
   ```

   The package depends on `cloudpickle`, `grpcio`, `packaging`, and `protobuf`.

2. If the service requires a token, set `READIE_AUTH_TOKEN` in the environment before starting your program.

   ```bash
   export READIE_AUTH_TOKEN="<your-token>"
   ```

   The client sends the token as an `authorization: Bearer ...` header on every call. If the variable is empty, the client sends no header. A wrong or missing token raises `PermissionDeniedError`. See [errors](/docs/guides/errors).

   The client reads `READIE_AUTH_TOKEN` when its `Settings` object is created. The value cannot be passed to `configure`.

3. Optional: to change the call timeout or the output behavior, call `readie.configure(...)` once, before the first remote call.

   ```python
   import readie

   readie.configure(timeout=120.0, stream_logs=False)
   ```

   `configure` accepts `timeout`, `chunk_size`, and `stream_logs`. See the [SDK reference](/docs/guides/sdk) for each parameter.

## Verify

Import the package and print its name:

```python
import readie

print(readie.__name__)
```

Expected output:

```text
readie
```

The client does not require a server address. It connects to the hosted Readie service on the first call to a `@remote` function.

## Troubleshoot

### IncompatiblePythonError

The local interpreter is not Python 3.12. Run the program with a Python 3.12 interpreter, for example in an environment created with `uv venv --python 3.12`. See [Troubleshooting](/docs/guides/troubleshooting#python-version-mismatch).

### PermissionDeniedError

The token is missing or wrong. Set `READIE_AUTH_TOKEN` before the client is created.

## What's next

- [Quickstart](/docs/getting-started/quickstart): run a first function remotely.
- [SDK reference](/docs/guides/sdk): all client settings and parameters.
