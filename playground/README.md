# readie-playground

The FastAPI service behind the playground on the docs site. A visitor submits
Python source; the service runs it on Readie twice in parallel, once with
checkpoint restore and once with `disable_optimized_execution=True` (cold
start), and streams both outputs back as server-sent events.

## Security model

The service interprets visitor code, so it is built to be contained. It runs under
gVisor (`runtime: runsc`) with no capabilities, a read-only root filesystem and a
small tmpfs. It uses the Readie client the way any user would, so it contacts the Readie
server that the client uses by default, through that server's nginx. Importing a visitor's
module runs only its decorator and `def`: the script is the body of `main`, which runs on
Readie. `SECURITY.md` has the full picture. Use a Readie token that only the playground has.

## Layout

| Path | Purpose |
| ---- | ------- |
| `src/readie_playground/app.py` | FastAPI app: `POST /run` (SSE), `GET /healthz`, CORS |
| `src/readie_playground/runner.py` | Two-lane parallel execution behind a `Backend` Protocol |
| `src/readie_playground/userfn.py` | Builds the visitor's script into a `@remote` function in a real module |
| `src/readie_playground/limits.py` | Per-client rate limit and concurrent-run cap |
| `src/readie_playground/config.py` | `PLAYGROUND_*` settings |
| `tests/` | Unit tests with a fake backend; no router needed |

## API

`POST /run` with `{"code": "<python source>"}` returns `text/event-stream`:

```text
event: log
data: {"lane": "optimized", "text": "hello\n"}

event: done
data: {"lane": "cold", "ok": true, "duration_s": 1.234, "error": "", "truncated": false}
```

Lanes are `optimized` and `cold`. Each lane ends with one `done` event. Errors
before streaming return 413 (too large), 422 (empty), 429 (rate limited) or 503
(busy).

### Evaluation route

`POST /eval/run` runs a list of steps (a notebook's cells, a debugging session)
once and returns every step's timing as JSON. The route has no token and is always
served, so nothing in the service keeps a visitor out of it. Both nginx configs answer
404 for `/playground/eval/` (a rule that sits before `/playground/` and must stay), so
the route is reachable only on the playground's own container network, at the
service's address on port 8080 (for example `http://<container address>:8080/eval/run`
from the host). It is meant for a harness run by the operator. Because it is not
behind nginx, nginx's request limits do not apply to it: a large request, a long run
and many requests at once are all accepted, and the route has no rate limit or
concurrency cap of its own.

The route makes no decisions. Apart from `steps` and `continue_on_error`, every
field is an argument of `@remote`, spelled as `@remote` spells it, and goes to
`readie.remote` untouched. A field left out keeps `remote`'s own default (the
playground adds no memory, deadline or session of its own).

```json
{
  "steps": ["import pandas as pd\nx = 41", "print(x + 1)"],
  "continue_on_error": true,
  "timeout": 300, "session": null,
  "memory": "8Gi", "max_memory": "8Gi",
  "gpu": false, "gpu_memory": null, "max_gpu_memory": null,
  "packages": [], "disable_optimized_execution": false, "checkpoint_id": null
}
```

Only `steps` is required. Unknown fields are refused with a 422, so a removed one
(`timeout_s`, `total_timeout_s`) is not ignored without a word. The arguments of
`@remote` are validated by `remote` itself, called once up front, so a bad size, a
bad requirement or `checkpoint_id` together with `disable_optimized_execution=true`
returns a 422 before anything runs. The route keeps its own limits on the request
(step count and code size, `PLAYGROUND_EVAL_*`) and no cap on any `@remote` value.
The set of fields follows `@remote`: when an argument is added or changed there,
`EvalRequest` changes with it (see `AGENTS.md`), and a test pins that they match.

- **One run per request.** Comparing an optimized start with a cold start is the
  caller's job: send the item twice, once with `disable_optimized_execution` false
  and once true.
- **Sessions.** `session` is passed as given. With none, every step is an independent
  call. With one, the steps share a container and an interpreter: names defined by one
  step are visible to the next. The route never invents a session id.
- **Steps run with module semantics** (`exec` in a namespace that lasts for the
  session, `__name__ == "__main__"`), so `from x import *`, classes and `global`
  behave as in a script. The function the
  client pickles repeats the step's import statements, so it records the same imports
  as a function you wrote and the router can pick a checkpoint. `MPLBACKEND=Agg`
  is set so plots need no display.
- **A failing step does not stop the others** unless `continue_on_error` is false,
  in which case the remaining steps are reported as skipped. A step that is not valid
  Python is reported without a call.

```json
{
  "session_id": "5f0c...",
  "ok": true,
  "total_s": 2.41,
  "steps": [{"index": 0, "ok": true, "duration_s": 1.93, "error": "",
             "error_type": "", "output": "", "truncated": false}]
}
```

`duration_s` is measured exactly as for `/run`: a monotonic clock around the whole
backend call (building the function, connecting, the call). `error_type` is
`RemoteExecutionError` for code that raised, `RemoteTimeoutError` when the deadline
passed, `SyntaxError`, `Skipped`, another Readie error name, or `InternalError`.
Errors before the run return 413 (too large) or 422 (invalid).

## Resource limits

Each lane builds the script into a function decorated with `@remote`, the way a user
would write one, and calls it through `readie.Client`. The module is a real file, so
the client reads the script's imports and the router can choose a checkpoint for the
optimized lane. The cold lane sets `disable_optimized_execution=True`.

Every `/run` asks for 1 GiB of initial and maximum memory (`config.MEMORY`) and
never uses a session, so each lane is a fresh sandbox that is destroyed after the call.
A request therefore uses two 1 GiB sandboxes. Size `WORKER_MAX_EXECUTORS` on the
Readie workers to match.

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `PLAYGROUND_HOST` / `PLAYGROUND_PORT` | `0.0.0.0` / `8080` | Listen address |
| `PLAYGROUND_ROUTER_URI` | empty | Readie server `host:port`. Empty uses the address the client uses by default |
| `READIE_AUTH_TOKEN` | empty | Bearer token for the router, read by the client |
| `PLAYGROUND_ALLOWED_ORIGINS` | `https://readie.org` | Comma-separated CORS origins |
| `PLAYGROUND_MAX_CODE_BYTES` | `16384` | Largest accepted source |
| `PLAYGROUND_MAX_OUTPUT_CHARS` | `65536` | Output kept per lane |
| `PLAYGROUND_TIMEOUT_S` | `30` | Deadline per run |
| `PLAYGROUND_RATE_LIMIT_REQUESTS` / `_WINDOW_S` | `10` / `60` | Runs allowed per client in each window |
| `PLAYGROUND_MAX_CONCURRENT_RUNS` | `4` | Simultaneous requests |
| `PLAYGROUND_TRUST_PROXY` | off | Use the first `X-Forwarded-For` address as the client (on behind nginx) |
| `PLAYGROUND_ROUTER_TLS` | `true` | Connect to the Readie server over TLS. Turn off only for a local plaintext server |
| `PLAYGROUND_WORKDIR` | `/tmp/playground` | Where generated modules are written. Must be writable |
| `PLAYGROUND_EVAL_MAX_CODE_BYTES` | `8388608` | Largest total source of one evaluation request |
| `PLAYGROUND_EVAL_MAX_STEPS` | `1000` | Most steps in one request |

Point the service at a dedicated Readie server with `SANDBOX_NETWORK=none` on its workers
rather than the one real users share, by setting `PLAYGROUND_ROUTER_URI`.

## Run it

The service is part of the compose stack, so `make run-local` and `make run-prod` start it.
It runs under gVisor, so the host must register the `runsc` runtime with Docker. Set `PLAYGROUND_AUTH_TOKEN` if the router requires a token.

```bash
make run-local
curl -N -X POST localhost:50051/playground/run \
  -H 'content-type: application/json' -d '{"code": "print(1)"}'
```

nginx routes `/playground/` to the service and everything else to the router. The
workers need enough memory for two 1 GiB sandboxes per request, for example
`READIE_WORKER_MEM_TOTAL=24Gi`.

## Deploy

- The host is a Linux amd64 machine with Docker and the gVisor `runsc` binary registered as a
  Docker runtime named `runsc` in `/etc/docker/daemon.json`.
- The Readie server the playground calls needs memory for two 1 GiB sandboxes per request
  (`READIE_WORKER_MEM_TOTAL`, `READIE_WORKER_MAX_EXECUTORS`). A dedicated server is
  recommended, with a token that only the playground uses.
- gVisor cannot use Docker's built-in DNS, so the service mounts `resolv.conf` from this
  directory. Replace its name server if the host blocks 8.8.8.8.
- The docs site finds the service through `PLAYGROUND_URL`, set when the site is built, for
  example `PLAYGROUND_URL=https://<host>/playground make docs-build`. The Run button stays
  disabled when it is empty. Set `PLAYGROUND_ALLOWED_ORIGINS` on the service to the docs
  site's origin.
- nginx limits requests to `/playground/` as a backstop, and the service enforces the
  limits that visitors see (see the table above).

## Develop

```bash
make install   # uv sync
make lint type test
make run       # outside gVisor; for development only
```
