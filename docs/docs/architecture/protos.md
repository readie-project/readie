---
title: Protobuf contracts
sidebar_position: 9
description: The gRPC services and messages that connect the client, router, and worker.
---


:::note Generated page
This page is generated from the comments in [`protos/`](https://github.com/illinoisdata/readie/tree/main/protos). To change it, edit the `.proto` files and run `make docs-protos`.
:::

The protos declare no `package`, so service names are bare on the wire, for example `/ProxyService/RequestExecution`. Changes are additive only: new fields use new numbers, and numbers are never reused or renumbered. See [Changing protos](/docs/contributing/changing-protos).

## `execution.proto`

### Service `ExecutionService`

Service on the workers to handle execution requests

| Method | Request | Response | Description |
| ------ | ------- | -------- | ----------- |
| `RequestExecution` | `stream WorkerExecutionRequest` | `stream WorkerExecutionResponse` | Bi-directional streaming RPC for streaming code execution input and output |

### Message `WorkerExecutionRequest`

Message used for code execution requests to workers

| Field | Type | Description |
| ----- | ---- | ----------- |
| `request_id` | `string` |  |
| `session_id` | `string` |  |
| `worker_id` | `string` |  |
| `container_id` | `optional string` |  |
| `checkpoint_id` | `string` |  |
| `payload` | `bytes` |  |
| `resources` | `repeated string` | Import and artefact hints from the client's static analysis, forwarded by the router so a worker can pick a checkpoint that already has them. |
| `budgets` | `repeated ResourceBudget` | The resource budgets (memory, GPU memory, ...) the router placed this request on. The worker sets each at container create and auto-expands up to the budget's max where the runtime can enforce it. |

### Message `WorkerExecutionResponse`

Message used for code execution responses from workers

| Field | Type | Description |
| ----- | ---- | ----------- |
| `request_id` | `string` |  |
| `session_id` | `string` |  |
| `worker_id` | `string` |  |
| `container_id` | `string` |  |
| `checkpoint_id` | `string` |  |
| `success` | `bool` |  |
| `logs` | `string` |  |
| `payload` | `bytes` |  |
| `budgets` | `repeated ResourceBudget` | The budgets the container actually ran with (post auto-expand). |

## `proxy.proto`

### Service `ProxyService`

Service on the router to handle execution requests from external clients

| Method | Request | Response | Description |
| ------ | ------- | -------- | ----------- |
| `RequestExecution` | `stream ClientExecutionRequest` | `stream ClientExecutionResponse` | Bi-directional streaming RPC for streaming code execution input and output |

### Message `ClientExecutionRequest`

Message used for code execution requests from clients

| Field | Type | Description |
| ----- | ---- | ----------- |
| `request_id` | `string` |  |
| `session_id` | `string` |  |
| `payload` | `bytes` |  |
| `config` | `ExecutionConfig` |  |

### Message `ClientExecutionResponse`

Message used for code execution responses to clients

| Field | Type | Description |
| ----- | ---- | ----------- |
| `request_id` | `string` |  |
| `session_id` | `string` |  |
| `success` | `bool` |  |
| `logs` | `string` |  |
| `payload` | `bytes` |  |
| `worker_id` | `string` | Where the execution actually ran. Attribution only: a client cannot ask for a particular worker or container, but it can report which one was slow. |
| `container_id` | `string` |  |

### Message `ExecutionConfig`

Per-call configuration the client sends as the first message of a stream.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `imports` | `repeated string` | Top-level module names the function refers to, from the client's static analysis. Forwarded to the worker so it can pick a checkpoint that already imported them. Attribution/selection only: not a memory hint. |
| `budgets` | `repeated ResourceBudget` | Resource budgets the user set on the decorator (memory, GPU memory, ...). A budget with alloc == 0 asks the router to apply its default for that kind. |
| `gpu` | `bool` | Whether the function needs a GPU. The router also treats any GPU-memory budget as a GPU request, so this covers a GPU function that names no size. |
| `disable_optimized_execution` | `bool` | When true, the router skips checkpoint restore for this call and always cold-starts. Session affinity (warm-container reuse) still applies; the router rejects this only when the session's warm container was itself restored from a checkpoint. |

## `registry.proto`

### Service `RegistryService`

Service on the router to handle status and utilization updates from workers and executors

| Method | Request | Response | Description |
| ------ | ------- | -------- | ----------- |
| `PostWorkerStatus` | `WorkerStatus` | `RegistryUpdateResponse` | Unary RPC for posting worker status |
| `PostExecutorStatus` | `ExecutorStatus` | `RegistryUpdateResponse` | Unary RPC for posting executor status |
| `PostWorkerUtilization` | `WorkerUtilization` | `RegistryUpdateResponse` | Unary RPC for posting worker utilization |
| `PostExecutorUtilization` | `ExecutorUtilization` | `RegistryUpdateResponse` | Unary RPC for posting executor utilization |

### Message `ExecutorStatus`

Message used for posting executor status

| Field | Type | Description |
| ----- | ---- | ----------- |
| `container_id` | `string` |  |
| `worker_id` | `string` |  |
| `request_id` | `string` |  |
| `session_id` | `string` |  |
| `status` | `Status` |  |

### Message `ExecutorUtilization`

Message used for posting executor utilization

| Field | Type | Description |
| ----- | ---- | ----------- |
| `container_id` | `string` |  |
| `worker_id` | `string` |  |
| `cpu_util` | `int64` |  |
| `cpu_total` | `int64` |  |
| `gpu_util` | `int64` |  |
| `gpu_total` | `int64` |  |

### Message `RegistryUpdateResponse`



| Field | Type | Description |
| ----- | ---- | ----------- |
| `updated` | `bool` |  |

### Message `WorkerStatus`

Message used for posting worker status

| Field | Type | Description |
| ----- | ---- | ----------- |
| `worker_id` | `string` |  |
| `worker_uri` | `string` |  |
| `status` | `Status` |  |
| `mem_total` | `int64` | Capacity, stamped on every status so a router that restarts relearns it from the next report rather than scheduling blind until the worker happens to re-register.

bytes this worker will hand out to executors |
| `max_executors` | `int32` | concurrent containers, 0 meaning unbounded |
| `flavor` | `string` | "cpu" (CPU-only) or "gpu" (CPU + GPU). The router routes GPU requests only to gpu workers and prefers cpu workers for CPU requests. |
| `gpu_mem_total` | `int64` | GPU device memory, in bytes, this worker offers |

### Message `WorkerUtilization`

Message used for posting worker utilization

| Field | Type | Description |
| ----- | ---- | ----------- |
| `worker_id` | `string` |  |
| `cpu_util` | `int64` |  |
| `cpu_total` | `int64` |  |
| `gpu_util` | `int64` |  |
| `gpu_total` | `int64` |  |
| `mem_used` | `int64` | Memory is what actually bounds how many executors fit on a worker, so the scheduler scores on it. Until these existed the worker sent only executor utilization and worker-level load did not exist at all.

bytes reserved by live executors |
| `mem_total` | `int64` | bytes available to executors |
| `executor_count` | `int32` | live containers right now |
| `gpu_mem_used` | `int64` | GPU device memory, so the router can place GPU requests on headroom the same way it does system memory. |
| `gpu_mem_total` | `int64` |  |

### Enum `Status`



| Name | Number | Description |
| ---- | ------ | ----------- |
| `STATUS_UNKNOWN` | 0 |  |
| `STATUS_READY` | 2 |  |
| `STATUS_BUSY` | 3 |  |
| `STATUS_ERROR` | 4 |  |
| `STATUS_REMOVED` | 5 |  |

## `resources.proto`

### Message `ResourceBudget`

A single resource budget: what to start with and how far it may grow.

Both are byte counts. `alloc` is the initial reservation the router places on
and the worker sets at container create; `0` means "unset, apply the default".
`max` is the auto-expand ceiling; `0` means "bounded only by worker capacity".

| Field | Type | Description |
| ----- | ---- | ----------- |
| `kind` | `ResourceKind` |  |
| `alloc` | `int64` |  |
| `max` | `int64` |  |

### Enum `ResourceKind`

A resource whose budget a caller can set per function. Kept deliberately
generic: a new dimension is one enum value here, one capacity source in the
router, and (where the runtime can enforce it) one grower in the worker.

| Name | Number | Description |
| ---- | ------ | ----------- |
| `RESOURCE_KIND_UNKNOWN` | 0 |  |
| `RESOURCE_KIND_MEMORY` | 1 | system RAM, in bytes |
| `RESOURCE_KIND_GPU_MEMORY` | 2 | GPU device memory, in bytes |

