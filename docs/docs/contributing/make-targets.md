---
title: Make targets
sidebar_position: 11
description: Root Makefile targets and variables, including which targets require an amd64 gVisor host.
---

The `Makefile` in the repository root is the single entry point for building images, running the checks for every component, and starting the stack. Run `make help` to print the annotated targets.

Run all commands from the repository root. Pass variables on the command line, for example `make generation FLAVOR=gpu`.

## Variables

| Variable | Default | Description |
| --- | --- | --- |
| `FLAVOR` | `cpu` | Which generation to build: `cpu` or `gpu`. Every artifact path and image name includes the flavor, so the two flavors do not overwrite each other. See [flavors and catalogues](/docs/concepts/flavors-and-catalogues). |
| `TAG` | UTC timestamp (`YYYYMMDD-HHMMSS`) | Tag added to the worker image, alongside `latest`. |
| `ANALYZE_EXCLUDE` | `google` | Space-separated packages `make analyze` skips, with everything under them. |
| `READIE_PLANNER` | `greedy` | Checkpoint planner: `greedy` or `fixed`. |
| `READIE_MAX_CHECKPOINTS` | `15` | Most checkpoints a plan may produce. |
| `READIE_SIZE_BUDGET_MB` | `2048.0` | Total size budget for checkpoints, in MB. |
| `READIE_ALPHA` | set in the Makefile | Size-versus-time weight. See [cost model](/docs/concepts/cost-model). |

The four `READIE_*` planner values are set with `:=` in the Makefile. A value passed on the command line takes precedence, for example `make capture READIE_ALPHA=<seconds-per-mb>`. A value that is only exported in the shell does not.

These values can differ from the pipeline's own defaults (for example 8 checkpoints), because `make capture` always passes its own values to the pipeline. See [Configuration reference](/docs/contributing/configuration-reference#pipeline).

The following names are derived from `FLAVOR`:

| Name | Value |
| --- | --- |
| `ARTIFACTS_DIR` | `pipeline/out/<flavor>` |
| `CATALOGUE_DIR` | `catalogues` |
| `PIPELINE_IMAGE` | `readie-pipeline-<flavor>:latest` |
| `ANALYZER_IMAGE` | `readie-pipeline-analyzer-<flavor>:latest` |
| `WORKER_BASE` | `readie-worker-base-<flavor>:latest` |
| `WORKER_IMAGE` | `readie-worker-<flavor>` |
| `ROOTFS_BASE_IMAGE` | `gcr.io/kaggle-images/python` for `cpu`, `gcr.io/kaggle-gpu-images/python` for `gpu` |

## Everyday targets

| Target | What it does |
| --- | --- |
| `all` | Generate protos, then lint, type-check and test everything. |
| `install` | Sync every Python virtual environment from its lockfile and download Go modules. |
| `lint` | Lint every component, and lint and format-check the protos. |
| `type` | Type-check the four Python components. The Go worker has no `type` target. |
| `test` | Test every component. The worker tests run with the race detector. Most tests use fakes and run anywhere. |
| `help` | List the annotated targets. |

The command `make lint type test` is the full check that CI repeats.

### Per-component targets

A pattern rule forwards `<component>-<verb>` to that component's own Makefile. The components are `router`, `pkg`, `executor`, `pipeline` and `worker`.

```bash
make worker-test      # same as: make -C worker test
make router-lint
make pkg-type
make pipeline-test
```

See [Repo map](/docs/contributing/repo-map) for the contents of each component.

## Proto targets

Generated code is committed. After editing a `.proto` file, run `make protos` and commit the result. See [Changing protos](/docs/contributing/changing-protos).

| Target | What it does |
| --- | --- |
| `protos` | Regenerate all gRPC stubs (Python and Go). |
| `protos-python` | Regenerate the router and client Python stubs. |
| `protos-go` | Regenerate the worker's Go stubs. |
| `protos-lint` | Lint and format-check the protos with `buf`. |
| `protos-fmt` | Format the protos in place. |
| `protos-breaking` | Check the protos for breaking changes against `main`. |
| `clean-protos` | Remove the generated Python stubs. |

## Checkpoint build targets

These targets build the images that hold checkpoints and require Docker. Targets with `yes` or `amd64 only` in the gVisor column work only on an amd64 Linux host with real gVisor, and do not work on Apple Silicon or in CI. For the procedure, see [Build checkpoints](/docs/contributing/build-checkpoints).

| Target | gVisor | What it does |
| --- | --- | --- |
| `pipeline-image` | no | Build the offline pipeline image. |
| `analyzer-image` | amd64 only | Build the analyzer image (the pipeline on the base image itself). |
| `analyze` | amd64 only | Measure every package the flavor's base image installs. Writes `pipeline/data/metadata/<flavor>.json`. Runs with no network. |
| `capture` | yes | Plan and capture checkpoints into `pipeline/out/<flavor>`. Runs a privileged container. Fails if no manifest is produced, then copies the catalogue to `catalogues/<flavor>.json`. |
| `worker-base` | needs captured checkpoints | Build the base image the worker runs on: gVisor, the root filesystem and the checkpoints. Prints how many checkpoints it baked. |
| `worker-image` | no | Build the worker image on top of the base. This is a Go build and takes seconds. Builds the base first if it is missing. |
| `generation` | yes | Run `capture`, `worker-base` and `worker-image` in order. |
| `clean-artifacts` | no | Delete captured checkpoints and the manifest for the flavor. |

To redeploy a worker code change without capturing again, run `make worker-image` alone, then recreate the container.

## Run targets

These targets wrap Docker Compose. See [Run the stack locally](/docs/contributing/run-the-stack) and [Deploy with TLS](/docs/contributing/deploy-with-tls).

| Target | What it does |
| --- | --- |
| `run-local` | Start NGINX, the router and a worker with `docker-compose.yml` plus `docker-compose.local.yml` (plaintext on port 50051). |
| `run-prod` | Same, with `docker-compose.prod.yml` (TLS on port 443). |
| `shutdown` | Stop the containers with `docker compose down --remove-orphans`. |

## Documentation targets

The documentation site has its own targets in the root Makefile:

| Target | What it does |
| --- | --- |
| `docs-install` | Install the documentation site's npm dependencies (`npm ci`). Needs Node 20 or newer. |
| `docs-tools` | Install `protoc-gen-doc`, the generator behind the protobuf reference page. |
| `docs-protos` | Regenerate the protobuf reference page (`architecture/protos`) from `protos/*.proto`. Needs `protoc` and `protoc-gen-doc` on your `PATH`. |
| `docs-build` | Build the static site. Fails on any broken link or anchor. |
| `docs-start` | Run the site with live reload. |
| `docs-serve` | Build the site, then serve the result locally. |

## See also

- [Configuration reference](/docs/contributing/configuration-reference)
- [Build checkpoints](/docs/contributing/build-checkpoints)
- [Run the stack locally](/docs/contributing/run-the-stack)
