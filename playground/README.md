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
through both lanes and returns every step's timing as JSON. It exists only when
`PLAYGROUND_EVAL_TOKEN` is set, needs `Authorization: Bearer <token>`, is not
published by nginx, and is meant for a harness run by the operator.

```json
{
  "steps": ["import pandas as pd\nx = 41", "print(x + 1)"],
  "session": null,
  "memory": "8Gi", "max_memory": "8Gi", "timeout_s": 300, "total_timeout_s": 300,
  "gpu": false, "gpu_memory": null, "max_gpu_memory": null,
  "packages": [],
  "continue_on_error": true
}
```

Only `steps` is required. The other fields are the options of the Readie remote
function, and each defaults to the playground's own value (1 GiB, 30 seconds, no
GPU, no packages, no total budget). `timeout_s` is the deadline of each step;
`total_timeout_s` is an optional budget for all the steps of one lane: a step gets
what is left of it, and once it is spent the remaining steps are reported as `OutOfTime`
without a call. The service caps memory, timeouts, step count and code size
(`PLAYGROUND_EVAL_*`).

- **Both lanes run at once, as `/run` does, each lane's steps one after another.** The
  optimized lane sends `disable_optimized_execution=False` and the cold lane
  `True` on every step. The harness sends one request per item and may have several in flight, up to `PLAYGROUND_EVAL_MAX_CONCURRENT_RUNS`.
- **Sessions.** More than one step, or a `session` id, runs each lane under its
  own session (`<id>-optimized`, `<id>-cold`), so its steps share one container
  and one interpreter: names defined by one step are visible to the next. A single
  step with no session is a plain call. The lanes never share a session, because
  an optimized session cannot be asked to start cold.
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
  "lanes": {
    "optimized": {
      "session_id": "5f0c...-optimized", "ok": true, "total_s": 2.41,
      "steps": [{"index": 0, "ok": true, "duration_s": 1.93, "error": "",
                 "error_type": "", "output": "", "truncated": false}]
    },
    "cold": {"session_id": "5f0c...-cold", "ok": true, "total_s": 6.02, "steps": ["..."]}
  }
}
```

`duration_s` is measured exactly as for `/run`: a monotonic clock around the whole
backend call (building the function, connecting, the call). `error_type` is
`RemoteExecutionError` for code that raised, `RemoteTimeoutError` when the deadline
passed, `OutOfTime` when the total budget was used up, `SyntaxError`, `Skipped`, another Readie error name, or `InternalError`.
Errors before the run return 401 (token), 413 (too large), 422 (invalid) or 503
(an evaluation is already running).

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
| `PLAYGROUND_EVAL_TOKEN` | empty | Bearer token for `/eval/run`. Empty does not serve the route |
| `PLAYGROUND_EVAL_MAX_CODE_BYTES` | `8388608` | Largest total source of one evaluation request |
| `PLAYGROUND_EVAL_MAX_STEPS` | `1000` | Most steps in one request |
| `PLAYGROUND_EVAL_MAX_TIMEOUT_S` | `1800` | Largest `timeout_s` a request may ask for |
| `PLAYGROUND_EVAL_MAX_MEMORY` | `64Gi` | Largest `memory` or `max_memory` a request may ask for |
| `PLAYGROUND_EVAL_MAX_CONCURRENT_RUNS` | `1` | Evaluation requests at once; another gets 503 |

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
