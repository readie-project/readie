# AGENTS.md

Orientation for coding agents working in this repository. It is intentionally
short: it maps the tree, lists the commands you run most often, and flags the
rules that are frequently violated. For setup, rationale, and the full conventions,
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
| `router/`   | Python 3.13  | Placement, cluster registry                                               |
| `worker/`   | Go 1.25      | Sandbox lifecycle, checkpoint/restore, executor I/O                       |
| `executor/` | Python 3.12  | Runs _inside_ every sandbox; unpickles and calls the function             |
| `pkg/`      | Python 3.12  | `readie-client`, the `@remote` SDK (imported and distributed as `readie`) |
| `pipeline/` | Python 3.13  | Offline: corpus, package analysis, checkpoint planning and capture        |
| `playground/` | Python 3.12 | FastAPI service behind the docs playground; runs visitor code under gVisor; calls Readie like any client |
| `protos/`   | protobuf     | Wire contracts: `execution`, `proxy`, `registry`, `resources`             |
| `docs/`     | Docusaurus   | Docs site (readie.org); Node pinned in `.nvmrc`                           |

Supporting: `.github/` (CI, CODEOWNERS, PR template), `catalogues/` (per-flavor
router catalogues), `docker-compose.yml`, and the umbrella root `Makefile`.

## Everyday commands

Run these from the repo root; the root `Makefile` fans out to every component.

- `make install`: sync every Python venv with `uv`, and run `go mod download`.
- `make lint type test`: the full gate, mirroring CI. (`type` covers the five
  Python components; the Go worker has no `type` target, and its lint includes vet.)
- Per-component passthroughs: `make worker-test`, `make router-lint`,
  `make pkg-type`, `make pipeline-test`, and so on (equivalently `make -C <component> <verb>`).
- `make protos`: regenerate all gRPC stubs after editing `protos/*.proto`.
- `make docs-protos`: regenerate the docs site's protobuf reference page after
  the same edit (needs `protoc-gen-doc`; see `make docs-tools`). The `docs`
  workflow fails on drift.
- `make help`: list annotated targets.

The Go worker runs under the race detector: `make worker-test` is `go test -race`.
Most tests use fakes and run anywhere (see gVisor note below).

## Gotchas that bite agents

- **Protos declare no `package` on purpose.** Service names are bare on the wire
  (`/ProxyService/RequestExecution`); the compose healthcheck `grpcurl`, the
  worker, and a router test depend on it, and `buf.yaml` enforces it. Do not add a
  package declaration.
- **Generated code is committed.** After changing `protos/*.proto`, run
  `make protos` and commit the diff. CI (`protos` job) and the `proto-stubs-current`
  pre-commit hook fail on drift. `proxy.proto` is Python-only (not generated for Go).
- **Proto changes are additive-only.** Use new field numbers, and never reuse or
  renumber one. `buf breaking` runs against `main` on PRs.
- **gVisor/runsc is amd64-only.** `make generation` and the worker's
  `make test-e2e` need a real amd64 gVisor host and do not run on Apple Silicon or
  in CI. Regular `make test` substitutes fakes and runs anywhere.
- **Alpha is shared through the catalogue.** The pipeline
  plans with `READIE_ALPHA` and writes the measured value into `catalogue.json`;
  the router has no `alpha` setting and reads it from that file. Deploy a new
  generation and its catalogue together.
- **The executor protocol has two implementations.** Python (`executor/`) and Go
  (worker) both implement it, and a cross-language golden fixture
  (`executor/tests/data/frames.golden.json`) locks them together. Change both sides
  and regenerate the golden; the `golden` CI job checks for drift.
- **The Python floors are deliberate.** `pkg` and `executor` target 3.12 because the
  client and the sandbox must run the same minor version, and the sandbox rootfs
  ships Python 3.12. `router` and `pipeline` target 3.13 because they control their
  own images.
- **`pkg` checks the Python version.** Functions are cloudpickled
  client-side and unpickled in the sandbox, and that is only safe within one minor
  version. `readie.Client()` calls `check_python_version()` (`pkg/src/readie/_compat.py`)
  and raises `IncompatiblePythonError` unless the local interpreter is exactly
  `REQUIRED_PYTHON` (3.12). If the executor's Python changes, update
  `REQUIRED_PYTHON`, `requires-python` in `pkg/pyproject.toml` and the
  executor's together.

## Conventions

Brief here; [`CONTRIBUTING.md`](CONTRIBUTING.md) is authoritative.

- Constructor dependency injection throughout. Seams are `typing.Protocol`
  (Python) or interfaces (Go), not ABCs.
- Test names describe the behaviour; a test pinning a past bug says so in a comment.
- Commit subjects: imperative mood, ≤~72 chars, no trailing period. Explain in the
  body what was broken and why the fix is shaped as it is.
- Do not commit or push unless asked.
- Documentation: the public site lives in `docs/` (see `docs/README.md` for the
  writing style). Write the product name as "Readie" in prose, keep user-facing
  pages free of development and operator detail, and do not state `alpha` values.
- Before opening a PR: `make lint type test` green; `go test -race ./...` if the
  worker changed; `make protos` rerun and committed if protos changed.

## Documentation

Documentation is part of the change. A pull request that changes behavior, a
setting, a default, a command, or a public name updates the docs that describe it
in the same change. When you find a doc that disagrees with the code, the code is
the source of truth: fix the doc, and say so in the commit body.

There are three kinds of documentation, each with one audience:

- **The site in `docs/`** (readie.org). Written for people who use, understand or
  contribute to Readie. Two groups: Get started and Guides are client-focused,
  for SDK users, and assume the hosted service, so they contain no router address,
  TLS setup, `router_uri`, or operator and development steps. Concepts,
  Architecture and Contribute are for contributors and developers: Concepts and
  Architecture explain the system and its design, and Contribute holds development
  and operator material (the glossary lives in Concepts).
  The site does not describe the playground (its architecture, concepts or
  deployment). That material lives in `playground/README.md` and `SECURITY.md`.
- **Component READMEs** (`router/`, `worker/`, `executor/`, `pkg/`, `pipeline/`,
  `playground/`, `nginx/`, `protos/`, `catalogues/`). Written for maintainers of that directory:
  layout, internals, configuration, how to test. `pkg/README.md` is the PyPI page
  and is written for SDK users.
- **Root files** (`README.md`, `CONTRIBUTING.md`, `SECURITY.md`, this file). Short,
  and they link to the others instead of repeating them.

### Which docs to update

| If you change | Update |
| ------------- | ------ |
| A `.proto` file | `make protos` and `make docs-protos`; `protos/README.md` if a rule changes |
| The executor wire protocol | `executor/README.md`; site `architecture/executor-protocol` and `contributing/changing-executor-protocol` |
| A setting or environment variable (router, worker, executor, pipeline) | The component README; site `contributing/configuration-reference` and `contributing/configure-services` |
| The SDK: `@remote` parameters, `Settings`, exceptions, messages | `pkg/README.md`; site `guides/sdk`, `guides/errors`, and the Get started pages that mention it |
| A Makefile target or variable | Its `##` help text; site `contributing/make-targets` |
| The catalogue format or how the router selects a checkpoint | `catalogues/README.md`; site `concepts/flavors-and-catalogues`, `concepts/cost-model`, `architecture/placement` |
| Sandbox or container behavior (network, limits, idle timeout, lifecycle) | `worker/README.md`; site `concepts/sandboxes`, `architecture/container-lifecycle` |
| Pipeline stages, outputs or commands | `pipeline/README.md`; site `architecture/building-checkpoints`, `contributing/build-checkpoints` |
| The playground service, its settings or its deployment | `playground/README.md` and `SECURITY.md`. The site does not describe the playground's architecture or concepts |
| Trust boundaries, auth, TLS or network defaults | `SECURITY.md`; site `architecture/security-model`, `architecture/security`, `contributing/deploy-with-tls` |
| CI jobs or the PR checklist | `CONTRIBUTING.md`; site `contributing/pull-requests` |
| A Python version floor | This file, `CONTRIBUTING.md`, site `getting-started/installation`, `getting-started/faq`, `guides/troubleshooting` |
| An error message users see | Site `guides/troubleshooting` and `guides/errors` |

If a change adds a page to the site, add it to the right folder in `docs/docs/`,
give it a `sidebar_position`, and link to it from a neighboring page.

### How to write it

The full rules are in [`docs/README.md`](docs/README.md) under "Writing style".
The ones that matter most:

- Write like reference documentation, not like a conversation. Open with a lead
  paragraph that defines the subject. Use the present tense and the active voice.
  Start steps with an imperative verb.
- Do not use contractions, "we", "please", "simply", "just" or "easy". Do not use
  rhetorical questions, or contrast fragments such as "Not X, not Y".
- Use sentence-case headings. Give each page one purpose: concept, task,
  reference or tutorial.
- Write the product name as "Readie" in prose. Environment variables, packages and
  identifiers keep their literal names (`READIE_AUTH_TOKEN`, `readie`).
- Do not write values that change, such as the `alpha` weight, image sizes or
  timings, unless the page says how they were measured. Point to where the value
  lives instead.
- Tag every code fence with a language. Show expected output in a separate fence.
- Check each claim against the code before writing it. Do not copy a claim from a
  README without checking it.

### Check before you finish

1. `make docs-build` passes. It fails on any broken link or anchor. Use the Node
   version in `docs/.nvmrc`.
2. `make docs-protos` leaves no diff if you touched a proto.
3. Search the docs for the name or value you changed (for example
   `grep -rn "SANDBOX_IDLE_TTL" README.md */README.md docs/docs`) and update every
   hit.
4. Single-source values stay single-source: the package version is written only in
   `pkg/pyproject.toml`, and `readie.__version__` reads it.

## Where to read more

- [`CONTRIBUTING.md`](CONTRIBUTING.md): setup, conventions, and "what is
  deliberately not done".
- [`SECURITY.md`](SECURITY.md): trust boundaries and deployment posture.
- Each component's `README.md`: its internals and configuration, including `protos/` and `catalogues/`.
- [`docs/README.md`](docs/README.md): the documentation site and its writing style.
- `worker/README.md` (Testing section): the four Go test tiers and their seams.
