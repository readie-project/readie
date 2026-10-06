---
title: Configuration reference
sidebar_position: 10
description: Environment variables for the router, worker, executor, and pipeline, with defaults and effects.
---

This page lists every setting that the router, worker, executor, and pipeline read from their environment, with the default for each. For guidance on which settings to change first, see [Configure the services](/docs/contributing/configure-services).

## Conventions

- The default is the value used when the variable is unset.
- Durations in the router are plain numbers of seconds (`30`, `2.5`). Durations in the worker are Go duration strings (`500ms`, `30s`, `5m`, `1h`).
- Sizes in the worker accept a byte count or a suffix: `512Mi`, `4Gi`, `4GB`. The suffixes `Ki`, `Mi`, `Gi`, and `Ti` are powers of 1024. The suffixes `K`, `M`, `G`, `T`, `KB`, `MB`, `GB`, and `TB` are powers of 1000.
- Booleans in the worker accept `true`/`false`, `1`/`0`, and `t`/`f`.
- An invalid value stops the component at startup with an error that names the variable.

## Router

The router reads its settings from environment variables, or from a `.env` file in its working directory. Names are not case-sensitive. Unknown variables are ignored. The router validates settings once at startup and does not change them while it runs.

### Transport

| Variable | Default | Description |
| --- | --- | --- |
| `SERVICE_NAME` | `router` | Name the router advertises to peers (`<name>:<port>`). The image sets `router`. |
| `PORT` | `50051` | gRPC port. `0` lets the OS choose a free port. |
| `BIND_HOST` | `0.0.0.0` | Address the server binds. |
| `MAX_CONCURRENT_RPCS` | unlimited | Cap on simultaneous gRPC calls. |
| `MAX_MESSAGE_BYTES` | `16777216` (16 MiB) | Largest gRPC message in either direction. |
| `AUTH_TOKEN` | empty (off) | When set, client calls must carry `authorization: Bearer <token>`. See [Deploy with TLS](/docs/contributing/deploy-with-tls). |

### Placement

| Variable | Default | Description |
| --- | --- | --- |
| `MEMORY_HEADROOM` | `0.9` | Fraction (above 0, up to 1) of a worker's memory the router will commit. |
| `DEFAULT_MEMORY` | `1073741824` (1 GiB) | Memory, in bytes, given to a call whose `@remote` sets none. Accepts plain bytes only. |
| `CATALOGUE_DIR` | empty | Folder of `<flavor>.json` checkpoint catalogues. Empty means no checkpoint selection, so every call cold-starts. The compose file sets it to `/var/lib/readie/catalogues`. See [flavors and catalogues](/docs/concepts/flavors-and-catalogues). |
| `SESSION_WAIT_TIMEOUT` | `60` | Seconds a call waits for its turn when its session is already busy. |
| `EXECUTION_TIMEOUT` | `3600` | Seconds an execution may run before it times out. |

### Liveness and clean-up

| Variable | Default | Description |
| --- | --- | --- |
| `PROBE_INTERVAL` | `5` | Seconds between health probes of each worker. |
| `PROBE_TIMEOUT` | `2` | Seconds one probe may take. |
| `PROBE_FAILURE_THRESHOLD` | `3` | Failed probes in a row before a worker is removed. |
| `REAPER_INTERVAL` | `5` | Seconds between clean-up sweeps. |
| `WORKER_TTL` | `30` | Seconds without contact before a worker is dropped. |
| `EXECUTOR_TTL` | `600` | Seconds an idle sandbox record is kept before the router forgets it. |
| `EXECUTOR_ERROR_TTL` | `60` | Seconds a sandbox record in an error state is kept. |
| `SHUTDOWN_GRACE` | `25` | Seconds to finish in-flight calls on shutdown. |

### Logging

| Variable | Default | Description |
| --- | --- | --- |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error`. |
| `LOG_FORMAT` | `json` | `json` or `console`. |

The router image also sets `APP_ENV=production`. The router does not read it.

## Worker

The worker reads environment variables only, plus a `.env` file in its working directory when `APP_ENV` is not `production`. The image sets `APP_ENV=production`, so the file is skipped in containers.

### Identity and connections

| Variable | Default | Description |
| --- | --- | --- |
| `PORT` | required (image: `50052`) | gRPC port. |
| `SERVICE_NAME` | required (image: `worker`) | Hostname the router uses to reach this worker, combined with `PORT`. |
| `ROUTER_URI` | required (image: `router:50051`) | The router's address, for registration. |
| `WORKER_DIR` | required (image: `/shared`) | Absolute path for per-sandbox folders and bundles. A volume. |
| `WORKER_ID` | `worker-1` | Unique ID the worker registers under. Must be unique for each worker in a fleet. |
| `WORKER_FLAVOR` | `cpu` (image build arg) | `cpu` or `gpu`. Sent to the router, which sends GPU calls only to `gpu` workers. |

The checkpoint and root filesystem location is fixed at `/var/lib/readie` inside the image. It cannot be changed with a setting.

### Capacity

| Variable | Default | Description |
| --- | --- | --- |
| `WORKER_MEM_TOTAL` | `4Gi` | Memory the worker offers to the router. Must match the memory the container actually has. |
| `WORKER_GPU_TOTAL` | `0` | GPU memory the worker offers. Set it on `gpu` workers. |
| `WORKER_MAX_EXECUTORS` | `0` (unlimited) | Most sandboxes at once. |
| `WORKER_UTILIZATION_INTERVAL` | `10s` | How often the worker reports load to the router. |

### Memory growth

| Variable | Default | Description |
| --- | --- | --- |
| `DEFAULT_CONTAINER_MEM` | `1Gi` | Memory limit for a call that carries no budget. |
| `MEM_GROWTH_THRESHOLD` | `0.9` | Fraction of the limit in use that triggers an increase. Must be above 0 and at most 1. |
| `MEM_GROWTH_FACTOR` | `2.0` | The limit is multiplied by this when it grows. Must be above 1. |

Growth stops at the call's `max_memory`, or at the worker's capacity when the call sets none.

### Sandbox runtime

| Variable | Default | Description |
| --- | --- | --- |
| `RUNSC_BINARY` | `/usr/local/bin/runsc` | Path to the gVisor binary. Absolute. |
| `RUNSC_ROOT` | `/run/readie-runsc` | gVisor's state directory, owned by this worker. Absolute. |
| `SANDBOX_NETWORK` | `sandbox` | `none`, `sandbox` or `host`, passed to gVisor as `--network`. |
| `SANDBOX_HOST_UDS` | `create` | Whether a socket made inside the sandbox is visible on the host. The executor needs it. |
| `SANDBOX_OVERLAY` | `root:memory` | Copy-on-write layer over the shared root filesystem. Values starting `all:` or containing `:self` are rejected. |
| `SANDBOX_PLATFORM` | empty | gVisor platform. Empty uses gVisor's default. |
| `SANDBOX_IGNORE_CGROUPS` | `false` | Skip cgroup enforcement. Compose sets `true` through `READIE_IGNORE_CGROUPS`. |
| `SANDBOX_GPU` | `true` on a `gpu` worker, otherwise `false` | Enables gVisor's NVIDIA proxy in the sandbox. |
| `SANDBOX_DEBUG` | `false` | Turns on gVisor debug logging. |
| `SANDBOX_DEBUG_LOG_DIR` | `/var/log/runsc` | Where that debug log goes. |
| `CGROUP_PARENT` | `/readie` | Parent cgroup for sandboxes. |
| `SANDBOX_IDLE_TTL` | `5m` | How long a finished session's sandbox stays warm before it is destroyed. `0` or negative turns reaping off. |

`SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY` and `SANDBOX_GPU` are part of a checkpoint's compatibility check. They must match the values the checkpoints were captured with. See [Checkpoints](/docs/concepts/checkpoints).

Each sandbox is also limited to half a CPU (quota 50000 per period 100000) and 100 processes. These two limits are fixed in the code.

### Checkpoints

| Variable | Default | Description |
| --- | --- | --- |
| `CHECKPOINT_STRICT_COMPAT` | `true` | If the baked checkpoints do not match this worker, `true` drops them (cold starts only, logged as an error). `false` keeps them and logs a warning, so restores that fail fall back to cold starts. |
| `CHECKPOINT_TIMEOUT` | `5m` | Longest a checkpoint operation may take. |

### Timeouts

| Variable | Default | Description |
| --- | --- | --- |
| `EXECUTION_TIMEOUT` | `1h` | Longest one execution may run. |
| `DIAL_TOTAL_TIMEOUT` | `60s` | Total time to connect to a sandbox's executor. |
| `RESPONSE_IDLE_TIMEOUT` | `30s` | Longest silence from the executor while a result is streaming. |
| `RUNSC_COMMAND_TIMEOUT` | `30s` | Time limit for one gVisor command. |
| `CONTAINER_STOP_TIMEOUT` | `2s` | Time a sandbox gets to stop. |
| `CLEANUP_TIMEOUT` | `30s` | Time allowed for clean-up work. |
| `SHUTDOWN_TIMEOUT` | `30s` | Time allowed for shutdown. |

### Streaming and logging

| Variable | Default | Description |
| --- | --- | --- |
| `STREAM_LOGS` | `true` | Read by the worker but has no effect: function output reaches the client inside the result, not as a live stream. |
| `STREAM_STATS` | `true` | Stream resource statistics. |
| `STATS_INTERVAL` | `1s` | Statistics interval. |
| `LOG_LEVEL` | `info` | A Go `slog` level: `debug`, `info`, `warn`, `error`. The local compose file sets `debug`. |
| `LOG_FORMAT` | `json` | `json` or `text`. |

## Executor

The executor runs inside the sandbox. Its environment is set by the sandbox spec that the worker and the pipeline write, so they rarely need to be set manually.

| Variable | Default | Description |
| --- | --- | --- |
| `EXECUTOR_DIR` | required | Folder where the executor makes its socket. The spec sets it. The executor refuses to start without it. |
| `EXECUTOR_SOCKET_NAME` | `executor.sock` | Socket file name. |
| `EXECUTOR_CHUNK_SIZE` | `1048576` (1 MiB) | Bytes per read and write on the socket. Positive integer. |
| `READIE_PREIMPORT` | empty | Comma-separated modules imported before the checkpoint is taken. This is how a checkpoint ends up with libraries already loaded. |
| `EXECUTOR_MODE` | `sandbox` | `capture`, `measure` or `sandbox`. Any other value falls back to `sandbox`. |

## Pipeline

The pipeline builds checkpoints offline. Settings come from environment variables, and command-line flags override them. The root `make` targets pass some of these values with different defaults from the table below. See [Make targets](/docs/contributing/make-targets).

| Variable | Default | Description |
| --- | --- | --- |
| `FLAVOR` (or `READIE_FLAVOR`) | `cpu` | `cpu` or `gpu`. Which generation to build. |
| `READIE_PLANNER` | `greedy` | Planner: `greedy` (adds the checkpoint that lowers total cost most, round by round) or `fixed`. |
| `READIE_MAX_CHECKPOINTS` | `8` | Most checkpoints a plan may produce. |
| `READIE_SIZE_BUDGET_MB` | `2048.0` | Total checkpoint size budget in MB. |
| `READIE_ALPHA` | built-in default | Size-versus-time weight in seconds per MB, rounded to 6 decimals. Must be positive. See [cost model](/docs/concepts/cost-model). |
| `READIE_CHECKPOINT_TIMEOUT` | `300.0` | Seconds a sandbox may take to checkpoint. |
| `READIE_DATA_DIR` | the pipeline's `data/` folder | Corpus and package metadata. |
| `READIE_PLAN_DIR` | empty (use the output folder) | Where the plan and spec fingerprint go. |
| `BASE_DIR` | `/app/executorfs` | The sandbox bundle (config plus root filesystem). |
| `EXECUTOR_DIR` | `/app/executor` | Host-side output folder for checkpoints and the manifest. |
| `ROOTFS_PYTHONPATH` | `/lib/python3.12/dist-packages` | Python path inside the root filesystem. |
| `SANDBOX_NETWORK` | `sandbox` | Must match the worker. |
| `SANDBOX_HOST_UDS` | `create` | Must match the worker. |
| `SANDBOX_OVERLAY` | `root:memory` | Must match the worker. Same restrictions as above. |
| `OCISPEC_BINARY` | `/usr/local/bin/ocispec` | Path to the spec generator. |
| `RUNSC_BINARY` | `runsc` | gVisor binary used for capture. |

Corpus generation also requires a hosted model. This authoring step is not needed to build checkpoints.

| Variable | Default | Description |
| --- | --- | --- |
| `AZURE_ENDPOINT` | required | Model endpoint. |
| `AZURE_API_KEY` | required | API key. Keep it out of version control. |
| `AZURE_MODEL_NAME` | required | Model or deployment name. |
| `AZURE_API_VERSION` | `2025-03-01-preview` | API version. |

## Shared planner constant

The planner constant `READIE_ALPHA` is shared between the pipeline and the router. The router reads the value from the catalogue that the pipeline writes, so it is not set on the router.

## See also

- [Configure the services](/docs/contributing/configure-services)
- [Make targets](/docs/contributing/make-targets)
- [Build checkpoints](/docs/contributing/build-checkpoints)
