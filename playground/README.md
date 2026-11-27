# readie-playground

The FastAPI service for HTTP execution and the playground on the docs site.
Every request runs once and streams output as server-sent events. The docs
frontend compares optimized and cold starts by sending two requests.

## Security model

The service interprets visitor code, so it is built to be contained. It runs under
gVisor (`runtime: runsc`) with no capabilities, a read-only root filesystem and a
small tmpfs. It uses the Readie client the way any user would, so it contacts the Readie
server that the client uses by default, through that server's nginx. The generated
module defines a wrapper with the visitor source stored as a string; the wrapper
executes that source on Readie. `SECURITY.md` has the full picture. Use a Readie
token that only the playground has.

## Layout

| Path | Purpose |
| ---- | ------- |
| `src/readie_playground/app.py` | FastAPI app: `POST /run` (SSE), `/mcp`, health, CORS |
| `src/readie_playground/execution.py` | `ReadieExecutionRequest` and `Admission`: the checks both front ends share |
| `src/readie_playground/mcp_server.py` | MCP server at `/mcp`: `run_python` and `new_session` |
| `src/readie_playground/runner.py` | Single execution behind a `Backend` Protocol |
| `src/readie_playground/userfn.py` | Builds the visitor's script into a `@remote` function in a real module |
| `src/readie_playground/config.py` | `PLAYGROUND_*` settings |
| `tests/` | Unit tests with a fake backend; no router needed |

## API

`POST /run` accepts `code` and the arguments of `@remote` and streams one execution
as server-sent events. Code-only requests use SDK defaults. The docs playground
makes two independent calls, one optimized and one cold, with a deadline on each.

See [Run code over HTTP](../docs/docs/guides/http-execution.md) for cURL
examples, fields and responses. `/eval/run` is removed; evaluation callers must
use `/run` and consume its SSE response.

The request model rejects unknown fields and validates remote options before
streaming. Sessions use caller-supplied identifiers and preserve script state.
No session is invented when `session` is missing or empty.

### MCP

`/mcp` serves the same executions to AI agents through the Model Context Protocol (MCP),
over streamable HTTP. The route is stateless and answers in JSON, so no MCP session is
kept between requests and any replica can serve any request. The Readie plugin for Claude
Code (`readie-project/readie-plugins`) connects to it, and so can any other MCP client
that supports streamable HTTP. The route has two tools:

- `run_python` takes the fields of `ReadieExecutionRequest` as its parameters, one for
  one, and a test compares the two. It returns the printed output and the duration as
  text. A failed execution or a rejected request is returned as a tool error that carries
  the traceback or the reason.
- `new_session` returns an unpredictable session identifier in the client's `sess-<hex>`
  format.

Both routes call `Admission.admit` (`execution.py`). As a result, `/run` and `run_python`
apply the same code-size and empty-code checks and the same option validation, and they
share one rate limiter per client address. Listing tools and creating session
identifiers run no code and are not counted. Both routes then run the code through the
same `Runner`.

A tool call sends its whole response when the execution ends, and nothing before it.
A proxy in front of `/mcp` therefore needs a read timeout at least as long as the longest
execution. The nginx configurations set one hour.

The service limits each client to a number of requests per window and answers 429 past it.
It adds no memory budget or concurrent-request cap. Code-size and output-size limits
remain. NGINX retains its request-body limit and proxy timeout. Worker capacity and
configured sandbox limits still apply.

## Configuration

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `PLAYGROUND_HOST` / `PLAYGROUND_PORT` | `0.0.0.0` / `8080` | Listen address |
| `PLAYGROUND_ROUTER_URI` | empty | Readie server `host:port`. Empty uses the address the client uses by default |
| `READIE_AUTH_TOKEN` | empty | Bearer token for the router, read by the client |
| `PLAYGROUND_ALLOWED_ORIGINS` | `https://readie.org` | Comma-separated CORS origins |
| `PLAYGROUND_MAX_CODE_BYTES` | `16384` | Largest accepted source |
| `PLAYGROUND_MAX_OUTPUT_CHARS` | `65536` | Output kept per execution |
| `PLAYGROUND_RATE_LIMIT_REQUESTS` / `_WINDOW_S` | `20` / `60` | Requests allowed per client in each window |
| `PLAYGROUND_TRUST_PROXY` | off | Use the first `X-Forwarded-For` address as the client (on behind nginx) |
| `PLAYGROUND_ROUTER_TLS` | `true` | Connect to the Readie server over TLS. Turn off only for a local plaintext server |
| `PLAYGROUND_WORKDIR` | `/tmp/playground` | Where generated modules are written. Must be writable |

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

To try the MCP route locally, connect Claude Code to it:

```bash
claude mcp add --transport http readie-local http://localhost:50051/playground/mcp
```

nginx routes `/playground/` to the service and everything else to the router. In production,
the hostname `run.readie.org` serves only `POST /` (the `/run` route of the service), `/mcp`
(the MCP route) and `/healthz`. The workers need capacity for one sandbox per request and two per frontend comparison.

## Deploy

- The host is a Linux amd64 machine with Docker and the gVisor `runsc` binary registered as a
  Docker runtime named `runsc` in `/etc/docker/daemon.json`.
- Size `READIE_WORKER_MEM_TOTAL` and `READIE_WORKER_MAX_EXECUTORS` for the requested
  resource budgets and concurrent executions. A dedicated server is
  recommended, with a token that only the playground uses.
- gVisor cannot use Docker's built-in DNS, so the service mounts `resolv.conf` from this
  directory. Replace its name server if the host blocks 8.8.8.8.
- The docs site finds the service through `PLAYGROUND_URL`, set when the site is built, for
  example `PLAYGROUND_URL=https://run.readie.org make docs-build`. The value is the full URL of the
  execution endpoint (`https://<host>/playground/run` behind the router hostname), and the site
  reads `/healthz` from its origin. The Run button stays
  disabled when it is empty. Set `PLAYGROUND_ALLOWED_ORIGINS` on the service to the docs
  site's origin.
- nginx limits requests to `/playground/` as a backstop and caps request body size. The
  service enforces the per-client rate limit that visitors see (see the table above) and
  keeps code-size and output-size limits.

## Develop

```bash
make install   # uv sync
make lint type test
make run       # outside gVisor; for development only
```
