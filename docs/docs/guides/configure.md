---
title: Tune your calls
sidebar_position: 1
description: Settings for memory, sessions, timeouts, output, and snapshot restore on functions and on the client.
---

Readie works with its default settings. This page covers the settings that are most often changed, and the complete list is in the [SDK reference](/docs/guides/sdk). Settings apply in two places:

- **On the function:** arguments to `@remote(...)`, such as `memory`, `timeout`, and `packages`.
- **For the whole program:** `readie.configure(...)`, called once before the first remote call, and the `READIE_AUTH_TOKEN` environment variable if the service requires a token.

## Set memory per function

Set the memory budget on the function, because an incorrect budget has the largest effect on a call. The value `memory` is the starting limit, and `max_memory` is the ceiling the limit may grow to.

```python
from readie import remote

@remote(memory="2Gi", max_memory="8Gi")
def train(data):
    ...
```

If neither value is set, the call gets a default of 1 GiB. When a sandbox approaches its limit, the limit grows up to the ceiling. If a call appears to have run out of memory, it is retried once with a larger limit, provided there is room below the ceiling. For the size units, see [Packages and resources](/docs/getting-started/packages-and-resources).

## Keep state with a session

By default, every call gets a fresh sandbox. To keep something loaded between calls, such as a model, use a [session](/docs/concepts/sessions). The sandbox of a session is kept after each call so that the next call starts quickly. The worker removes it after it has been idle for a while (about five minutes by default). A session whose sandbox is gone fails with a session-expired error. See [Sessions and errors](/docs/getting-started/sessions-and-errors).

## Set timeouts for long calls

A call has no client-side deadline unless one is set. Pass `timeout` (in seconds) on the function, or set it for every call:

```python
@remote(timeout=900)
def long_job(...): ...
```

```python
import readie

readie.configure(timeout=900)
```

The service also enforces its own upper limit on the run time of a single call (an hour by default). A call that exceeds a deadline raises `RemoteTimeoutError`.

## Control output

The sandbox captures anything the function prints, and the client shows it on stderr after the call finishes. To turn this off:

```python
readie.configure(stream_logs=False)
```

## Disable snapshot restore

To force a plain cold start for one function, for example to compare timings, set `disable_optimized_execution`:

```python
@remote(disable_optimized_execution=True)
def baseline(...): ...
```

## Environment variables

| Variable | Description |
| --- | --- |
| `READIE_AUTH_TOKEN` | Bearer token, if the service requires one. The client reads it when it is created. |

## See also

- [SDK reference](/docs/guides/sdk): every option.
- [Packages and resources](/docs/getting-started/packages-and-resources): memory units and GPU budgets.
- [Troubleshooting](/docs/guides/troubleshooting): errors and fixes.
