---
title: Configure the services
sidebar_position: 10
description: Router, worker, and executor settings that operators change most often, with worked examples.
---

Use this guide to adjust the settings of the router, worker, and executor when you run the stack yourself. Callers that use `@remote` do not need it. They tune their calls as described in [Tune your calls](/docs/guides/configure).

The services run with their defaults. This guide covers the settings that operators change most often and the reason for each change. For every variable and its default, see the [Configuration reference](/docs/contributing/configuration-reference).

## Prerequisites

- A running stack. See [Run the stack locally](/docs/contributing/run-the-stack).
- Access to the environment of the router and worker containers. With Docker Compose, use the `environment:` block of the service or a compose override file.

The router and worker read environment variables. The executor environment is set by the sandbox spec and is rarely changed manually. An invalid value stops the router or worker at startup with a message that names the variable, so a service never runs half-configured.

## Set worker memory

Incorrect memory settings cause the most severe failures, so configure them first.

### Set the memory that the worker offers

`WORKER_MEM_TOTAL` is the memory that the worker offers to the router, and the router schedules calls against it. The worker does not measure the machine, because inside a container it would read the host's memory instead of the container's. If the value exceeds the memory of the container, the router places too much work on one worker, and a sandbox can be killed for running out of memory during a call.

The default is `4Gi`. With Compose, set the value in the `.env` file:

```bash
READIE_WORKER_MEM_TOTAL=16Gi
READIE_WORKER_MAX_EXECUTORS=8
```

`WORKER_MAX_EXECUTORS` caps the number of concurrent sandboxes. `0` means no cap.

The router also reserves headroom. `MEMORY_HEADROOM` (default `0.9`) limits the router to committing at most 90 percent of a worker's memory.

### Set the memory that callers request

Callers set `memory` and `max_memory` on their function. `memory` is the starting limit, and `max_memory` is the ceiling to which the worker can grow the limit. See [Packages and resources](/docs/getting-started/packages-and-resources).

```python
from readie import remote

@remote(memory="2Gi", max_memory="8Gi")
def train(data):
    ...
```

If a call sets neither value, it receives a default. `DEFAULT_MEMORY` on the router and `DEFAULT_CONTAINER_MEM` on the worker are both 1 GiB.

When a sandbox nears its limit, the worker grows the limit. Two settings control the growth:

- `MEM_GROWTH_THRESHOLD` (default `0.9`): the share of the limit in use that triggers growth.
- `MEM_GROWTH_FACTOR` (default `2.0`): the multiplier applied to the limit.

If a call appears to have run out of memory, the worker retries it once with a larger limit, provided there is room below the ceiling.

## Set the lifetime of warm sandboxes

A call that uses a [session](/docs/concepts/sessions) leaves its sandbox running so that the next call is fast. Two timers determine how long the sandbox remains:

| Setting | Component | Default | Effect |
| --- | --- | --- | --- |
| `SANDBOX_IDLE_TTL` | worker | `5m` | After this idle time, the worker destroys the sandbox. `0` turns the cleanup off. |
| `EXECUTOR_TTL` | router | `600` seconds | After this idle time, the router forgets the sandbox. |

To keep session sandboxes for 15 minutes, raise both values so that they stay in step:

```yaml
services:
  worker:
    environment:
      SANDBOX_IDLE_TTL: 15m
  router:
    environment:
      EXECUTOR_TTL: "900"
```

A longer lifetime holds memory for callers that might not return. A session whose sandbox is gone fails with a session-expired error. See [Sessions and errors](/docs/getting-started/sessions-and-errors).

## Set network access in the sandbox

The `SANDBOX_NETWORK` setting on the worker has three values:

| Value | Meaning |
| --- | --- |
| `sandbox` (default) | The sandbox has its own network stack with outside access. Package installs and downloads work. |
| `host` | The sandbox shares the host's network stack. |
| `none` | The sandbox has no network. |

The value is part of a checkpoint's compatibility check. Checkpoints are captured under one setting and restore only under the same setting. If you change the value on the worker, the checkpoints no longer match, and by default the worker drops them (see [Configure strict checkpoint checking](#configure-strict-checkpoint-checking)). The `make capture` target does not forward `SANDBOX_NETWORK`, so a custom value for the pipeline requires a change to how the pipeline runs. Keep the value at `sandbox` unless you build and run your own generation.

## Configure strict checkpoint checking

When the worker starts, it compares its own sandbox settings and gVisor version with the values recorded when the checkpoints were captured.

- `CHECKPOINT_STRICT_COMPAT=true` (default): on a mismatch, the worker refuses the checkpoints, logs an error, and serves cold starts only. Calls succeed but start more slowly.
- `CHECKPOINT_STRICT_COMPAT=false`: the worker keeps the checkpoints and logs a warning. Restores that cannot work fall back to cold starts one call at a time.

Keep the setting `true` in production. Set it to `false` only for experiments, where a failed restore before every fallback is acceptable. The log lines are listed in [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack).

## Set timeouts for long calls

A long call can reach three separate limits. Raise every limit that applies.

| Limit | Component | Default |
| --- | --- | --- |
| Per-call `timeout` | caller (`@remote(timeout=...)`) | none |
| `EXECUTION_TIMEOUT` | router | 3600 seconds |
| `EXECUTION_TIMEOUT` | worker | `1h` |
| `grpc_read_timeout` and `grpc_send_timeout` | NGINX configuration | `1h` |

## Point the router at the catalogue

Without checkpoint catalogues, the router cold-starts every call. The compose file already points `CATALOGUE_DIR` at the mounted `catalogues/` folder. After you build new checkpoints, the `capture` step copies the catalogue there. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).

## Set logging

The router and the worker log JSON by default. To read logs while debugging, switch to a readable format and raise the verbosity:

| Component | Settings |
| --- | --- |
| Router | `LOG_LEVEL=debug`, `LOG_FORMAT=console` |
| Worker | `LOG_LEVEL=debug`, `LOG_FORMAT=text` |

The local compose file already sets the worker's level to `debug`.

## Executor and pipeline settings

The executor settings are not normally changed. The pipeline fills in the executor's `READIE_PREIMPORT` list, which determines the libraries that a checkpoint preloads. The pipeline's planner settings, such as the number of checkpoints and the size budget, are described in [Build checkpoints](/docs/contributing/build-checkpoints).

## Verify

After changing a setting, recreate the affected container and check its log for startup errors:

```bash
docker compose up -d router worker
docker compose logs router worker
```

A startup error names the invalid variable. A service that starts without an error accepted the new values.

## What's next

- [Configuration reference](/docs/contributing/configuration-reference)
- [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack)
- [Deploy with TLS](/docs/contributing/deploy-with-tls)
