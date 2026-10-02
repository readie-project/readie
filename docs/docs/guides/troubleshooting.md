---
title: Troubleshooting
sidebar_position: 4
description: Match an error raised by the Readie client to its cause and fix.
---

Find the error in the following table. Each row links to a longer explanation. Every exception class is listed in the [errors reference](/docs/guides/errors).

| Symptom | Likely cause | Section |
| --- | --- | --- |
| `Readie requires Python 3.12, found Python 3.x` | Wrong local Python version. | [Python version mismatch](#python-version-mismatch) |
| `ClusterUnavailableError` | Network problem, or the service is busy or down. | [Cannot connect](#cannot-connect) |
| `UNAUTHENTICATED ...: a valid bearer token is required` | Missing or wrong token. | [Auth rejected](#auth-rejected) |
| `NOT_FOUND ...: the session has expired` | The sandbox of the session was cleaned up. | [Session expired](#session-expired) |
| `RESOURCE_EXHAUSTED ...: the session is busy` | Another call held the session too long. | [Session expired](#session-expired) |
| The call fails, or appears to have run out of memory | The memory budget is too small. | [Out of memory](#out-of-memory) |
| `uv pip install failed (exit ...)` | Bad package specification, or the package cannot be fetched. | [Package install failures](#package-install-failures) |
| `RemoteTimeoutError` | The call exceeded its deadline. | [Timeouts](#timeouts) |

## Python version mismatch

Message: `Readie requires Python 3.12, found Python 3.13`, raised as `IncompatiblePythonError`.

The function is pickled on the local machine and unpickled by Python 3.12 inside the sandbox. This is safe only when both sides run the same minor version. The client checks the version when it creates a `Client`, which `@remote` does on the first call.

To fix the error, run the program with Python 3.12. Create a Python 3.12 virtual environment, for example with `uv venv --python 3.12`.

## Cannot connect

Message: `ClusterUnavailableError: ... is unavailable, or has no worker with capacity: <details>`

Two causes are possible:

1. **Network problem.** Check that the machine can reach the internet and that no firewall or proxy blocks outgoing gRPC (HTTP/2) traffic. The `<details>` part usually shows a gRPC connection failure.
2. **The service is busy or down.** Wait and retry. If the failure persists, check the project status or open an issue.

If the message is `RESOURCE_EXHAUSTED ...: no worker can accept this request`, no worker had room for the call. Retry later, or request less memory.

## Auth rejected

Message: `PermissionDeniedError: UNAUTHENTICATED from <host:port>: a valid bearer token is required`

The service requires a token, and the call did not carry a valid one. Set `READIE_AUTH_TOKEN` in the environment of the process that creates the client. The client reads the variable when it is created, so set it first. Passing the token as a `Settings` argument is not supported. See [Install the client](/docs/getting-started/installation#steps).

## Session expired

Message: `SessionExpiredError: NOT_FOUND from <host:port>: the session has expired`

The sandbox of the session was removed, usually because it was idle for too long (about five minutes by default). Open a new session and do not reuse the old `Session` object.

A related message is `the session is busy`, raised as `ResourceExhaustedError`. A session handles one call at a time. A second call waits for its turn (about a minute by default) and then fails. See [Sessions and errors](/docs/getting-started/sessions-and-errors).

## Out of memory

A call that uses more memory than its budget can fail. Readie retries once with a larger limit when there is room below `max_memory`. If the retry also fails, the original error is reported.

To fix the failure, set a larger budget on the function, with room to grow:

```python
@remote(memory="4Gi", max_memory="16Gi")
def heavy(...): ...
```

See [Packages and resources](/docs/getting-started/packages-and-resources).

## Package install failures

If `packages=[...]` is passed to `@remote`, the packages are installed with `uv` before every call. A failure ends the call with the following text, which also appears in the output:

```text
uv pip install failed (exit 1): <uv output>
```

The usual causes are:

- **The name or version does not exist.** Read the `uv` output for the package that it could not find.
- **The package cannot be fetched.** The output shows a failure to reach the package index. Retry, and if the failure persists, try a different version.

Entries that are not valid requirement strings are rejected on the local machine, before the call, with `InvalidPackageError`. Packages are installed on every call and nothing is cached, so a large install slows every call. Prefer libraries that are already in the sandbox. See [Packages and resources](/docs/getting-started/packages-and-resources).

## Timeouts

The message `RemoteTimeoutError: DEADLINE_EXCEEDED from ...: the execution timed out` means the call exceeded a deadline. For a long call, pass a longer `timeout`. See [Tune your calls](/docs/guides/configure#set-timeouts-for-long-calls).

## Get help

If the problem remains, see the [FAQ](/docs/getting-started/faq). Alternatively, [open an issue](https://github.com/illinoisdata/readie/issues/new/choose) that includes the full error message and the Python version.
