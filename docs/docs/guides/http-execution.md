---
title: Send HTTP execution requests
sidebar_position: 5
description: Run Python code on Readie with cURL and remote execution options.
---

`POST /playground/run` executes Python source on Readie and returns server-sent
events (SSE). Send requests with cURL or another HTTP client; the Python SDK is
not required. Each request runs the code once. Include `@remote` options to choose
resources, install packages, set a deadline or reuse a session.

## Send one execution

Send a JSON body containing `code` and the execution options. This example runs
once with checkpoint optimization, a memory budget and a deadline:

```bash
curl --no-buffer --fail-with-body \
  'https://readie.eastus.cloudapp.azure.com/playground/run' \
  -H 'Content-Type: application/json' \
  --data '{"code":"print(42)","disable_optimized_execution":false,"memory":"2Gi","max_memory":"4Gi","timeout":30}'
```

The response has content type `text/event-stream`:

```text
event: log
data: {"text": "42\n"}

event: done
data: {"ok": true, "duration_s": 0.123, "error": "", "truncated": false}
```

`duration_s` includes function construction, connection and execution; the value
shown is an example. To run once without checkpoint optimization, set
`disable_optimized_execution` to `true`. Both modes return the same event format.
Each request sends output events followed by one `done` event for its execution.

Omit execution options to use the SDK defaults.

To send a script from `script.py`, use `jq` to encode it as JSON:

```bash
jq -n --rawfile code script.py \
  '{code: $code, disable_optimized_execution: false, timeout: 30}' > request.json
curl --no-buffer --fail-with-body \
  'https://readie.eastus.cloudapp.azure.com/playground/run' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

## Request fields

`code` is required and cannot be empty or contain only whitespace. Unknown fields
are rejected. The other fields match `@remote`, with two exceptions: `session` is
a string identifier, and `client` is not accepted because it refers to a Python
client object.

| Field | JSON type | Default | Meaning |
| ----- | --------- | ------- | ------- |
| `code` | string | required | Python source |
| `timeout` | number or null | null | Whole-call deadline in seconds; null uses the SDK default, currently no deadline |
| `session` | string or null | null | Reuse a session's interpreter; an empty string means no session |
| `gpu` | boolean | false | Request GPU execution |
| `memory` | string, integer or null | null | Initial memory budget; integer values are bytes |
| `max_memory` | string, integer or null | null | Memory ceiling |
| `gpu_memory` | string, integer or null | null | Initial GPU memory budget |
| `max_gpu_memory` | string, integer or null | null | GPU memory ceiling |
| `packages` | array of strings or null | null | PEP 508 package requirements to install |
| `disable_optimized_execution` | boolean | false | Execute without checkpoint optimization |
| `checkpoint_id` | string or null | null | Pin a checkpoint; incompatible with disabling optimized execution |

Use size strings such as `2Gi` or an integer number of bytes for memory values.
The SDK validates the options before execution. There is no fixed playground
memory budget: omitted values use the cluster defaults. Worker capacity and
configured sandbox limits still apply.

Single executions run the code as a script, with `__name__ == "__main__"`.
Requests with the same session identifier share Python state while that session
exists. Send dependent session requests sequentially. Session identifiers have no
caller ownership check: anyone who knows an identifier can execute code in that
session. Avoid predictable identifiers and do not store secrets in shared sessions.

## Compare optimized and cold execution

Send two requests with the same code. Set `disable_optimized_execution` to `false`
for the optimized execution and `true` for the cold execution. Each request has
its own output stream and completion event. Omit `session` for independent runs.
The website playground sends these two requests itself and displays their results
side by side; the API does not run a comparison within one request.

## Handle results and errors

Read the stream until it sends `done`. Output arrives in `log` events; use
`print()` to return text from a script. The API does not return Python objects.
Each `done` event contains:

| Field | Meaning |
| ----- | ------- |
| `ok` | Whether execution succeeded |
| `duration_s` | Backend call duration in seconds |
| `error` | Execution failure details, or an empty string |
| `truncated` | Whether output exceeded the configured character limit |

HTTP 200 means the stream started, not that the code succeeded. Syntax errors,
execution errors and execution timeouts produce a `done` event with `ok: false`.
Check this field in automated clients: cURL's `--fail-with-body` only checks the
HTTP status.

Errors before streaming use a JSON `detail` response:

| Status | Meaning |
| ------ | ------- |
| 413 | Request body or source exceeds its size limit; NGINX may return its own error body |
| 422 | Missing or empty code, an unknown field, or invalid execution options |

Closing the stream cancels the active execution tasks. A proxy timeout can also
close the connection: NGINX currently allows 120 seconds without response data.
Set an execution deadline with `timeout` when a script may wait without producing
output.

## Access and limits

The public endpoint requires no HTTP authentication. It has no per-client rate
limit, application concurrency cap or fixed playground execution memory budget.
Cluster resource defaults
still apply when memory options are omitted.

Limits on code size, output size and HTTP request body size remain. A
`truncated: true` completion event means the output limit was reached.
