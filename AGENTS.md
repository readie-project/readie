# AGENTS.md

Orientation for coding agents working in this repository. It is intentionally
short: it maps the tree, lists the commands you will actually run, and flags the
rules that are easy to violate. For setup, rationale, and the full conventions,
read [`CONTRIBUTING.md`](CONTRIBUTING.md) and each component's `README.md`.

## What this is

A checkpoint/restore system for serverless Python. A user decorates a function
with `@remote` (the `readie` client in `pkg/`); the **router** places the call; a Go
**worker** restores a gVisor (runsc) checkpoint and runs the function inside the
sandbox via an in-sandbox **executor**. The **pipeline** builds those checkpoints
offline. Everything talks gRPC over contracts in `protos/`.

## Repo map

| Path        | Language     | Purpose                                                                   |
| ----------- | ------------ | ------------------------------------------------------------------------- |
| `nginx`     | NGINX        | Client-facing proxy                                                       |
| `router/`   | Python 3.12  | Placement, cluster registry                                               |
| `worker/`   | Go 1.25      | Sandbox lifecycle, checkpoint/restore, executor I/O                       |
| `executor/` | Python 3.12  | Runs _inside_ every sandbox; unpickles and calls the function             |
| `pkg/`      | Python 3.12  | `readie-client`, the `@remote` SDK (imported and distributed as `readie`) |
| `pipeline/` | Python 3.12  | Offline: corpus, package analysis, checkpoint planning and capture        |
| `protos/`   | protobuf     | Wire contracts: `execution`, `proxy`, `registry`, `resources`             |

Supporting: `.github/` (CI, CODEOWNERS, PR template), `catalogues/` (per-flavor
router catalogues), `docker-compose.yml`, and the umbrella root `Makefile`.

## Everyday commands

Run these from the repo root; the root `Makefile` fans out to every component.

- `make install` - sync every Python venv (via `uv`) and `go mod download`.
- `make lint type test` - the full gate, mirroring CI. (`type` covers the four
  Python components; the Go worker has no `type` target - its lint includes vet.)
- Per-component passthroughs: `make worker-test`, `make router-lint`,
  `make pkg-type`, `make pipeline-test`, … (equivalently `make -C <component> <verb>`).
- `make protos` - regenerate all gRPC stubs after editing `protos/*.proto`.
- `make help` - list annotated targets.

The Go worker runs under the race detector: `make worker-test` is `go test -race`.
Most tests use fakes and run anywhere (see gVisor note below).

## Gotchas that bite agents

- **Protos declare no `package` on purpose.** Service names are bare on the wire
  (`/ProxyService/RequestExecution`); the compose healthcheck `grpcurl`, the
  worker, and a router test depend on it, and `buf.yaml` enforces it. Do not add a
  package declaration.
- **Generated code is committed.** After changing `protos/*.proto`, run
  `make protos` and commit the diff - CI (`protos` job) and the `proto-stubs-current`
  pre-commit hook fail on drift. `proxy.proto` is Python-only (not generated for Go).
- **Proto changes are additive-only** - new field numbers, never reuse or
  renumber. `buf breaking` runs against `main` on PRs.
- **gVisor/runsc is amd64-only.** `make generation` and the worker's
  `make test-e2e` need a real amd64 gVisor host and won't run on Apple Silicon or
  in CI. Regular `make test` substitutes fakes and runs anywhere.
- **The `alpha` cost constant (0.002) is shared** and must match between the
  pipeline (`READIE_ALPHA`) and the router (`ALPHA`).
- **The executor wire protocol is dual-implemented** - Python (`executor/`) and Go
  (worker) - and locked by a cross-language golden fixture
  (`executor/tests/data/frames.golden.json`). Change both sides and regenerate the
  golden; the `golden` CI job checks for drift.
- **The two Python floors are deliberate:** `pkg` and `executor` target 3.11
  because they install into a user's process; `router` (3.13) and `pipeline` (3.12)
  control their own images.

## Conventions

Brief here; [`CONTRIBUTING.md`](CONTRIBUTING.md) is authoritative.

- Constructor dependency injection throughout. Seams are `typing.Protocol`
  (Python) or interfaces (Go), not ABCs.
- Test names describe the behaviour; a test pinning a past bug says so in a comment.
- Commit subjects: imperative mood, ≤~72 chars, no trailing period. Explain in the
  body what was broken and why the fix is shaped as it is.
- Don't commit or push unless asked.
- Before opening a PR: `make lint type test` green; `go test -race ./...` if the
  worker changed; `make protos` rerun and committed if protos changed.

## Where to read more

- [`CONTRIBUTING.md`](CONTRIBUTING.md) - setup, conventions, and "what is
  deliberately not done".
- [`SECURITY.md`](SECURITY.md) - trust boundaries and deployment posture.
- Each component's `README.md` - its internals and configuration.
- `worker/README.md` (Testing section) - the four Go test tiers and their seams.
