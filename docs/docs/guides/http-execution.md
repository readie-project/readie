---
title: Run code over HTTP
sidebar_position: 1
description: Run Python code on Readie with one HTTP request, without installing the client.
---

The HTTP execution endpoint runs Python source code on Readie and streams the output back as server-sent events (SSE). Any HTTP client can call it, so the Python client is not required. Each request runs the code once. To run code with the Python client instead, see the [Quickstart](/docs/getting-started/quickstart) and the [SDK reference](/docs/guides/sdk).

:::warning[Return values are not sent]
This method runs the code as a script and returns only the output that the code prints. It returns no result value, so an expression such as `1 + 1` produces no output. Use `print()` to return a result.
:::

## Example

Send a JSON body with the Python source code in the `code` field:

```bash
curl -N 'https://run.readie.org' \
  -H 'Content-Type: application/json' \
  --data '{"code": "print(\"Hello from Readie\")"}'
```

The response contains the output in `log` events, followed by one `done` event:

```text
event: log
data: {"text": "Hello from Readie\n"}

event: done
data: {"ok": true, "duration_s": 0.123, "error": "", "truncated": false}
```

## Endpoint

```text
POST https://run.readie.org
```

| Property | Value |
| -------- | ----- |
| Request content type | `application/json` |
| Response content type | `text/event-stream` |
| Authentication | None |

## Request

The request body is a JSON object. The `code` field is required and must contain more than whitespace. The other fields are optional and correspond to the arguments of `@remote` in the Python client, which the [SDK reference](/docs/guides/sdk) describes. The endpoint rejects fields that are not listed.

| Field | Type | Default | Description |
| ----- | ---- | ------- | ----------- |
| `code` | `string` | required | Python source code. |
| `timeout` | `number` or `null` | `3600` | Deadline for the whole call, in seconds. The execution ends with an error when the deadline passes. Default is 3600 seconds. |
| `session` | `string` or `null` | `null` | Session whose interpreter the request reuses. An empty string means no session. |
| `gpu` | `boolean` | `false` | Request GPU execution. |
| `memory` | `string`, `integer` or `null` | `"1Gi"` | Initial memory budget. Default is 1 GiB. An integer is a number of bytes. |
| `max_memory` | `string`, `integer` or `null` | `"4Gi"` | Memory ceiling. The memory of the sandbox grows from the initial budget up to this value when the code needs more. Default is 4 GiB. |
| `gpu_memory` | `string`, `integer` or `null` | `null` | Initial GPU memory budget. |
| `max_gpu_memory` | `string`, `integer` or `null` | `null` | GPU memory ceiling. |
| `packages` | `array of strings` or `null` | `[]` | Package requirements to install, in the [PEP 508](https://peps.python.org/pep-0508/) format. |
| `disable_optimized_execution` | `boolean` | `false` | Run without restoring a [checkpoint](/docs/concepts/checkpoints). |
| `checkpoint_id` | `string` or `null` | `null` | Checkpoint to restore. Readie chooses one when this is not set. Cannot be combined with `disable_optimized_execution`. |

Omit a field to use its default. The endpoint checks the options before it runs the code. An invalid option returns status 422.

### Sessions

Requests that name the same session share Python state while the session exists. See [Sessions](/docs/concepts/sessions). Send requests that depend on one another one at a time.

A session identifier has no owner: anyone who knows the identifier can run code in that session. Choose unpredictable identifiers and do not store secrets in a shared session.

## Response

The response is a stream of events. Read the stream until the `done` event arrives.

### The `log` event

A `log` event carries a piece of the output. The stream can contain any number of `log` events.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `text` | `string` | A piece of the output. |

### The `done` event

The `done` event is the last event of the stream.

| Field | Type | Description |
| ----- | ---- | ----------- |
| `ok` | `boolean` | Whether the execution succeeded. |
| `duration_s` | `number` | Time the execution took, in seconds. |
| `error` | `string` | Details of the failure, or an empty string. |
| `truncated` | `boolean` | Whether the output exceeded the character limit. |

Status 200 means that the stream started, not that the code succeeded. A syntax error, an execution error or an execution timeout produces a `done` event with `ok` set to `false`. Check this field in automated clients.

## Errors

An error that occurs before the stream starts returns a JSON body with a `detail` field:

| Status | Description |
| ------ | ------- |
| 413 | The request body or the source code exceeds its size limit. |
| 422 | The code is missing or empty, a field is unknown, or an execution option is invalid. |
| 429 | The client address sent too many requests. Wait, and send the request again. |

Closing the connection cancels the execution. A connection that receives no data for a long time can also be closed by the server. Set `timeout` when a script can run for a long time without printing.

## Limits

The endpoint accepts 20 requests every 60 seconds from each client address. Further requests receive status 429 until the period ends. The endpoint also limits the size of the request body, the size of the source code and the size of the output. A `done` event with `truncated` set to `true` means that the output reached its limit.

## More examples

### Set execution options

This request sets a memory budget and a deadline:

```bash
curl -N 'https://run.readie.org' \
  -H 'Content-Type: application/json' \
  --data '{"code": "print(\"Hello from Readie\")", "memory": "2Gi", "max_memory": "4Gi", "timeout": 30}'
```

The response has the same format as in the first example.

### Send a script file

Encode the contents of a file as a JSON string with `jq`, and send the result:

```bash
jq -n --rawfile code script.py '{code: $code, timeout: 30}' > request.json
curl -N 'https://run.readie.org' \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

## Related pages

- [SDK reference](/docs/guides/sdk): the arguments of `@remote`.
- [Use Readie in Claude](/docs/guides/claude-code): run code on Readie from Claude chat, Claude Code and other AI agents.
- [Sessions and errors](/docs/getting-started/sessions-and-errors): keep state between calls.
- [Packages and resources](/docs/getting-started/packages-and-resources): extra libraries, memory, and GPUs.
