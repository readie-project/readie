---
title: Troubleshoot the stack
sidebar_position: 12
description: Causes and fixes for symptoms in router and worker logs when you run the stack yourself.
---

Use this guide to diagnose problems in a stack that you operate: the router, the workers, and the sandboxes they run. Callers that see an error from Python start with [Troubleshooting](/docs/guides/troubleshooting).

## Prerequisites

- A running stack. See [Run the stack locally](/docs/contributing/run-the-stack).
- Access to the logs of the router and worker containers with `docker compose logs`.

## Symptoms

| Symptom | Likely cause | Section |
| --- | --- | --- |
| `no workers are available` | No worker registered | [No worker registered](#no-worker-registered) |
| Calls always start slowly | Cold starts instead of restores | [Restore or cold start](#restore-or-cold-start) |
| Worker log: `baked checkpoints are incompatible with this worker` | Sandbox settings or gVisor version changed | [Checkpoint compatibility failures](#checkpoint-compatibility-failures) |
| `execution failed as if out of memory` in the worker log | Memory budget too small | [Out of memory](#out-of-memory) |
| Downloads inside the function fail | Sandbox has no network | [No network in the sandbox](#no-network-in-the-sandbox) |
| Sandbox creation fails with `device or resource busy` | cgroup delegation blocked (Docker Desktop) | [Sandbox will not start](#sandbox-will-not-start) |

## No worker registered

Callers see the following message:

```text
ClusterUnavailableError: ... has no worker with capacity
```

Check the following causes in order.

1. No worker registered. The router answers with `no workers are available`. Check the worker log for `registered with the router` and `worker ready`. If the log contains `worker serving but unusable; every execution will be refused`, the worker is running but cannot execute anything. The error logged next to it states the reason, usually missing artifacts.
2. The stack is not running. Run `docker compose ps` and check that `nginx`, `router`, and `worker` are healthy. See [Run the stack locally](/docs/contributing/run-the-stack).
3. No worker has room. If the router has workers but none with free capacity, callers receive `RESOURCE_EXHAUSTED ...: no worker can accept this request`. Raise `WORKER_MEM_TOTAL` or `WORKER_MAX_EXECUTORS`, add a worker, or request less memory. See [Configure the services](/docs/contributing/configure-services).

## Session expired too soon

The worker removes a session's sandbox after it is idle for longer than `SANDBOX_IDLE_TTL` (worker, default 5 minutes). The router forgets it after `EXECUTOR_TTL` (router, default 600 seconds). To keep sessions longer, raise both values as described in [Configure the services](/docs/contributing/configure-services). A second call on a busy session waits up to `SESSION_WAIT_TIMEOUT` (default 60 seconds) and then fails.

## Restore or cold start

A **restore** loads a saved process image and is fast. A **cold start** boots a fresh sandbox and imports every package. See [cold starts and restore](/docs/concepts/cold-starts-and-restore).

To determine which path a call used, read the logs.

In the router log (`docker compose logs router`), the `execution placed` line includes `checkpoint_id` and `container_id`. An empty `checkpoint_id` means the router did not select a checkpoint. The line also carries `warm`, which indicates whether the call reused an existing sandbox.

In the worker log (`docker compose logs worker`), the following lines apply:

| Log line | Meaning |
| --- | --- |
| `container restored from checkpoint` (with `checkpoint_id`) | The call was restored. |
| `container started` | The call was a cold start. |
| `checkpoint restore failed, falling back to a cold start` | The restore failed and the worker cold-started instead. The `err` field gives the reason. |
| `cannot resolve the requested checkpoint; starting cold` | The router named a checkpoint that this worker does not have. |
| `artifacts loaded` | Logged at startup. Shows the number of checkpoints the worker has. `checkpoints=0` means every call cold-starts. |
| `no manifest; cold starts only` | The image has no checkpoint manifest. Build checkpoints first. |
| `skipping unusable checkpoint` | One checkpoint failed to load and was left out. |

If every call cold-starts, check the following in order:

1. The router has catalogues. Its log shows `loaded checkpoint catalogues`. If the line is missing, `CATALOGUE_DIR` is empty or the `catalogues/` folder has no `<flavor>.json`.
2. The worker has checkpoints. The `artifacts loaded` line shows a non-zero count.
3. The checkpoints are compatible with the worker. See [Checkpoint compatibility failures](#checkpoint-compatibility-failures).
4. The function does not set `disable_optimized_execution=True` on `@remote`, which tells the router to skip restores.

## Checkpoint compatibility failures

At startup, the worker compares its sandbox settings (network mode, overlay, GPU) and its gVisor version with the values recorded when the checkpoints were captured. A checkpoint restores only into the same kind of sandbox that it was captured from.

In strict mode (the default), the worker logs the following line:

```text
baked checkpoints are incompatible with this worker; refusing them and serving cold starts only
```

The `reasons` field states the difference, for example `captured under spec fingerprint ... but this worker builds ...` or `captured under runsc "..." but this worker runs "..."`.

If `CHECKPOINT_STRICT_COMPAT=false`, the worker logs the following line:

```text
baked checkpoints look incompatible with this worker; restores will fall back to cold starts
```

To fix the failure, make the worker match the checkpoints, or capture the checkpoints again under the worker's settings. The usual causes are a changed `SANDBOX_NETWORK`, `SANDBOX_OVERLAY`, or `SANDBOX_GPU`, or a different gVisor build. If the worker cannot compute its own fingerprint, it logs `cannot fingerprint this worker's sandbox; leaving baked checkpoints in place` and skips the check. Rebuilding checkpoints requires an amd64 gVisor host. See [Build checkpoints](/docs/contributing/build-checkpoints).

## Out of memory

The worker log contains the following message, with `from` and `to` sizes:

```text
execution failed as if out of memory; retrying with a larger limit
```

The worker doubles the limit once and retries, up to the caller's `max_memory`. A related line, `grew container memory`, appears when the worker raises the limit before the limit is reached. If the retry also fails, or there was no room to grow, the call fails and the original error is reported.

To fix the failure, set a larger budget on the function, with room to grow:

```python
@remote(memory="4Gi", max_memory="16Gi")
def heavy(...): ...
```

Also confirm that `WORKER_MEM_TOTAL` does not exceed the memory that the worker actually has. See [Configure the services](/docs/contributing/configure-services).

## No network in the sandbox

Package installs fail, or the function cannot download data. Check the following causes:

- The worker's `SANDBOX_NETWORK` is `none`. Set it to `sandbox` (the default). The setting must match the checkpoints, as described in [Checkpoint compatibility failures](#checkpoint-compatibility-failures).
- The worker container has no outbound access. Check Docker's network and the firewall.
- DNS resolution fails. The executor fills in `/etc/resolv.conf` before each call when the image ships it empty, so a DNS failure indicates a missing outbound route rather than a missing file.

## Sandbox will not start

Sandbox creation fails with `device or resource busy`. On Docker Desktop, cgroup delegation refuses what gVisor needs.

To fix the failure, set `READIE_IGNORE_CGROUPS=true` in `.env`, which is the compose default. The worker then runs with `SANDBOX_IGNORE_CGROUPS=true`. The worker runs only on an amd64 Linux host. See [Run the stack locally](/docs/contributing/run-the-stack).

## What's next

- [Configure the services](/docs/contributing/configure-services)
- [Build checkpoints](/docs/contributing/build-checkpoints)
- [Configuration reference](/docs/contributing/configuration-reference)
