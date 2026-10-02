---
title: Pull requests
sidebar_position: 6
description: Commit message format, pull request template, CI jobs, review process, and how to report a bug.
---

This page covers the steps between finishing a change and merging it: writing a commit message, completing the pull request template, and passing CI.

## Commit messages

- Write the subject line in the imperative mood: "Fix empty result error", not "Fixed" or "Fixes".
- Keep the subject to about 72 characters.
- Omit the full stop at the end of the subject.
- Use the body to explain what was broken and why the fix has its shape. The diff shows what changed, so the body does not repeat it.

The following invented example shows the format:

```text
Reject sessions that outlive their container

A session id kept pointing at a container the worker had already
reaped, so the next call failed with an opaque socket error. Check the
binding before dispatch and return a clear error instead.
```

## Before you open a pull request

CI runs only on pull requests that target `main` and on manual runs. It does not run on every push to a branch, so run the checks locally first. See [Set up your environment](/docs/contributing/setup).

```bash
make lint type test
```

Also run these checks when they apply:

- If the worker changed, run `go test -race ./...` from `worker/`.
- If a proto changed, run `make protos` and commit the result.

## Pull request template

GitHub fills in a template from `.github/PULL_REQUEST_TEMPLATE.md` when a pull request is opened. The template has these parts:

1. **What was broken.** The behavior before the change and why it was wrong.
2. **What this does.** The shape of the fix, and any alternative that was rejected.
3. **Verification.** What was run and what it printed. "Tests pass" is not sufficient. Name the tests and state what they cover that they did not cover before.
4. **A checklist:**
   - `make lint type test` is green for every component that was touched
   - `go test -race ./...` passes if the worker changed
   - `make protos` is rerun and committed if a proto changed
   - a test was added that fails without this change
5. **Contract changes.** Delete this section if there are none. Otherwise select what applies and state what an operator must do:
   - a proto field was added (new number, nothing renumbered or reused)
   - the executor protocol version was bumped, with both ends and the golden fixture updated
   - the generation manifest changed, so existing artifacts need regenerating
   - a configuration variable was added or renamed, and documented in the README of the component

State accurately what the checks prove. The sandbox path requires a real amd64 gVisor host. If that path was not run, say so.

## CI jobs

The workflow is `.github/workflows/ci.yml`. It defines these jobs:

| Job | What it does |
| --- | --- |
| `python` | Runs four times, once each for `router` (3.13), `pipeline` (3.13), `pkg` (3.12), and `executor` (3.12). Each run does `uv sync --all-groups`, then `make lint`, `make type`, and `make test`. |
| `worker` | In `worker/`: `go build ./...`, `go test -race ./...`, then `golangci-lint` at the version pinned in `worker/Makefile`. |
| `protos lint` | `buf lint`, `buf format --diff --exit-code`, and on pull requests `buf breaking` against `main`. |
| `generated stubs are current` | Installs the pinned generators, runs `make protos`, and fails if the working tree changed. |
| `framing fixture is current` | Regenerates `executor/tests/data/frames.golden.json` and fails if it changed. See [Change the executor protocol](/docs/contributing/changing-executor-protocol). |
| `images build` | Builds the router and worker images and checks the pipeline Dockerfile. This job is currently switched off. |

None of these jobs can run real gVisor. CI never tests capturing a checkpoint or executing a function in a sandbox.

A second workflow, `publish.yml`, publishes the `pkg` client to PyPI when a maintainer publishes a GitHub release. The release tag must match the version in `pkg/pyproject.toml`.

If a job fails, open its log and run the same command locally. Every command in the table is a `make` target or a one-line command.

## Reviews

The repository has a `CODEOWNERS` file, so GitHub requests review from the owners automatically. Dependabot also opens pull requests that update Go modules, `uv` dependencies, GitHub Actions, and Docker images.

## Report a bug

Open a [GitHub issue](https://github.com/illinoisdata/readie/issues/new/choose) and select **Bug report**. The template asks for the following:

- **Component:** router, worker, executor, pkg (client SDK), pipeline, or infrastructure.
- **What happened:** the exact error and the log lines around it. The router and worker write JSON logs with `request_id` and `container_id`, which allow one request to be followed across components.
- **What you expected.**
- **Reproduction:** the smallest example that shows the problem. For a remote function, include the body: what it imports and what it returns.
- **Environment:** host OS and CPU architecture, component version or commit, and whether the stack ran with `docker compose` or from source.

If the bug involves running a function, state whether the host is Apple Silicon. gVisor runs only on amd64, so the problem cannot be reproduced there.

For a feature request, select **Feature request** and describe the problem as well as the proposed solution. If the proposal touches protos or the executor protocol, say so, because the change affects more than one language.

For a security problem, do not open a public issue. See [Security](/docs/architecture/security).

## What's next

- [Conventions](/docs/contributing/conventions)
- [FAQ](/docs/getting-started/faq)
