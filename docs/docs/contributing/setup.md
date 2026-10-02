---
title: Set up your environment
sidebar_position: 1
description: Install the required tools, sync every component, and run the checks that CI runs.
---

This procedure takes a fresh clone to a passing `make lint type test`. Most tests use fakes, so no special server is required.

## Prerequisites

Install the following tools.

| Tool | Purpose |
| --- | --- |
| [uv](https://docs.astral.sh/uv/) | Creates and syncs a virtual environment for each Python component. |
| Go 1.25 or later | Builds and tests the worker. `worker/go.mod` declares `go 1.25.0`. |
| [buf](https://buf.build/docs/installation) | Lints and formats the `.proto` files. The root `make lint` runs it, so it is required even when you do not edit a proto. |
| [pre-commit](https://pre-commit.com/) | Runs the fast checks before each commit. |
| Docker | Builds images and runs the stack. Tests do not require it. |

Python 3.12 and 3.13 do not need a separate installation, because `uv` downloads the interpreter for each component. Each Python component selects its own version:

| Component | Python | Reason |
| --- | --- | --- |
| `pkg/` | 3.12 only (`~=3.12.0`) | Runs in the user's process and must match the sandbox. |
| `executor/` | 3.12 only (`~=3.12.0`) | Runs inside the sandbox image. |
| `router/` | 3.13 or later | Ships in its own image. |
| `pipeline/` | 3.13 | Pinned in `.python-version` and used by CI. Runs offline in its own image. |

For the reason `pkg` and `executor` are held at 3.12, see [Conventions](/docs/contributing/conventions).

## Steps

1. From the repository root, install every component.

   ```bash
   make install
   ```

   This command runs `uv sync --all-groups` in `router/`, `pkg/`, `executor/`, and `pipeline/`, then `go mod download` in `worker/`. Each Python environment is built from the lockfile of its component, so every contributor gets the same versions.

2. Install the pre-commit hooks.

   ```bash
   uv tool install pre-commit && pre-commit install
   ```

3. Run the full check.

   ```bash
   make lint type test
   ```

## Verify

The full check is the same gate that CI runs. It consists of three targets:

- `make lint` checks proto formatting with `buf`, runs `ruff format --check` and `ruff check` on each Python component, and runs `golangci-lint` on the worker.
- `make type` runs `mypy` in strict mode on the four Python components. The Go worker has no type target.
- `make test` runs `pytest` for the Python components and `go test -race` for the worker.

All three targets exit with status 0 when the setup is correct.

## Pre-commit hooks

After installation, every `git commit` runs the following checks:

- Basic file checks: trailing whitespace, YAML, TOML, and JSON syntax, merge-conflict markers, files over 2 MB, and private keys.
- `ruff check --fix` and `ruff format`.
- `gofmt` and `go vet`.
- `buf format` and `buf lint` when a proto changes.
- A check that the generated stubs are up to date when a proto changes.

Tests and type checks are not in the hooks because they take too long. CI runs them.

## Work on one component

Every component has its own `Makefile` with the same verbs. The two forms are equivalent:

```bash
make -C router test    # run it in the directory
make router-test       # run it from the root
```

The common verbs are `install`, `fmt`, `lint`, `type`, and `test`. The `fmt` verb applies formatting and safe lint fixes. The router also has `test-unit` and `test-integration`. The worker adds `make worker-test-short`, which skips slow tests, and `make worker-cover`, which writes a coverage report. Run `make help` at the root or inside a component to list every target. See [Make targets](/docs/contributing/make-targets) for the full list.

## What runs without a gVisor host

[gVisor](/docs/concepts/glossary) (`runsc`) runs only on Linux on amd64. It does not work on Apple Silicon or on CI runners that emulate another CPU.

| Command | gVisor host required |
| --- | --- |
| `make test` | No. The worker tests replace the real sandbox with fakes. |
| Python component tests | No. |
| `make -C worker test-e2e` | Yes. Runs the real-`runsc` tests on a Linux amd64 host with `runsc` installed. |
| `make generation` | Yes. Captures new checkpoints on a real amd64 gVisor host, and also needs Docker and a large amount of disk space. |

A passing `make test` does not prove that the sandbox path works. State in the pull request which of these commands you ran. See [Pull requests](/docs/contributing/pull-requests).

## Troubleshoot

### `golangci-lint` is missing

The worker lint step requires `golangci-lint`. Run `make -C worker tools`. The target installs the pinned `golangci-lint`, the protobuf code generators, and `protoc`.

## What's next

- [Repository map](/docs/contributing/repo-map)
- [Run locally](/docs/contributing/run-the-stack)
- [Conventions](/docs/contributing/conventions)
