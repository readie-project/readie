---
title: Build checkpoints
sidebar_position: 8
description: Measure packages, capture checkpoints, and build a worker image that contains them.
---

Use this procedure to build checkpoints and a worker image from them. For the internals of the pipeline, see [Building checkpoints](/docs/architecture/building-checkpoints). The [glossary](/docs/guides/glossary) defines the terms used here.

A worker without checkpoints still works, but every start is a cold start. Build checkpoints to obtain fast restores, and rebuild them when the package set, the base image, the gVisor version, or the executor changes.

## Prerequisites

- An amd64 Linux host with Docker. gVisor intercepts system calls, which breaks under emulation, and the base image is published for amd64 only. The procedure does not work on Apple Silicon or in standard CI runners.
- Permission to run privileged containers. Capture runs with `--privileged` and with AppArmor and seccomp turned off, because gVisor creates its own namespaces.
- A terminal. The capture command uses `docker run -it` and requires a TTY.
- For `FLAVOR=gpu`, the NVIDIA container toolkit, because capture adds `--gpus all`. See [Flavors and catalogues](/docs/concepts/flavors-and-catalogues).
- Network access to pull the Kaggle base image: `gcr.io/kaggle-images/python` for `cpu` and `gcr.io/kaggle-gpu-images/python` for `gpu`.
- Free disk space. The following table lists four sizes, each measuring a different item.

| Item | Size | Source |
| --- | --- | --- |
| Root filesystem layer (the base image's Python environment, copied into the worker base) | about 26 GB | Comments in `pipeline/Dockerfile` |
| `readie-pipeline-cpu` image (capture tool plus that filesystem) | 26.6 GB | `docker images` on a maintainer machine |
| Checkpoint files for one run with 101 checkpoints | 13 GB on disk (about 13 MB for the empty one; the rest range up to several hundred MB each) | `du` on that machine |
| `readie-worker-base-cpu` image (runsc, filesystem, and those checkpoints) | 40.1 GB | `docker images` on that machine |

A default `make` run plans at most 15 checkpoints, so the checkpoint size is smaller than for the 101-checkpoint run. Docker also keeps the build cache and older image tags. The numbers come from one machine and are not a guarantee.

## Run all steps with one command

To capture checkpoints and build both images in one run:

```bash
make generation FLAVOR=cpu
```

`FLAVOR` defaults to `cpu`. The target runs three steps in order and prints the final image tag:

1. `capture` builds the pipeline image, runs the planner, and captures checkpoints.
2. `worker-base` bakes runsc, the filesystem, and the checkpoints into one image.
3. `worker-image` adds the Go worker on top.

The target then prints a reminder to run `docker compose up -d`. Each step is also available as a separate target, as described in the following sections.

## Step 1: Measure packages

Run `make analyze` to measure the packages in the base image:

```bash
make analyze FLAVOR=cpu
```

The target builds an analyzer image directly from the Kaggle base image and imports every installed package in a fresh process. It runs with `--network none` so that cloud libraries do not stall while probing for credentials. The target writes `pipeline/data/metadata/cpu.json`, which can be reviewed and committed.

The repository already contains a measured file for `cpu`. Run this step again only if the base image's packages changed.

To skip packages, pass a space-separated list. The default excludes `google`:

```bash
make analyze ANALYZE_EXCLUDE="google.cloud.aiplatform foo"
```

## Step 2: Capture checkpoints

Run `make capture` to plan and capture the checkpoints:

```bash
make capture FLAVOR=cpu
```

The target builds the pipeline image and runs it as a privileged container. Inside the container, the pipeline plans which packages go into each checkpoint, captures each checkpoint, times a restore of each, and writes the outputs. The target stops with an error if no `manifest.json` appears, which prevents a checkpoint-less image from being baked unintentionally.

The planner settings can be overridden on the command line. The following values are the Makefile values, which differ from the pipeline's own defaults:

| Variable | Makefile value | Meaning |
| --- | --- | --- |
| `READIE_PLANNER` | `greedy` | Planner: `greedy` or `fixed`. |
| `READIE_MAX_CHECKPOINTS` | `15` | Maximum checkpoints in a plan. The empty checkpoint is added on top. |
| `READIE_SIZE_BUDGET_MB` | `2048.0` | Total memory budget across all checkpoints. |
| `READIE_ALPHA` | set in the Makefile | Seconds per MB that the planner charges for size. |

For example, to override the checkpoint count and the size weight:

```bash
make capture READIE_MAX_CHECKPOINTS=10 READIE_ALPHA=<seconds-per-mb>
```

## Step 3: Build the worker base image

Run `make worker-base` to bake the filesystem and checkpoints into the base image:

```bash
make worker-base FLAVOR=cpu
```

The target builds `readie-worker-base-cpu:latest` and prints how many checkpoints it baked. The step is slow because of the large filesystem layer. A base image built from an empty output directory works but serves only cold starts, and the printed count is the only indication, so check it.

## Step 4: Build the worker image

Run `make worker-image` to add the Go worker on top of the base image:

```bash
make worker-image FLAVOR=cpu
```

The target builds `readie-worker-cpu:<timestamp>` and `readie-worker-cpu:latest` from `worker/`. The step takes seconds because it adds only the Go server. If the base image is missing, the target builds it first. To name the tag, set `TAG=<tag-name>`.

After a change to worker code only, run this step alone. It does not capture checkpoints again.

## Output locations

| Output | Location |
| --- | --- |
| Checkpoints, `manifest.json`, `catalogue.json` | `pipeline/out/<flavor>/` (ignored by git) |
| Router catalogue | `catalogues/<flavor>.json`, copied from the pipeline output |
| Package measurements | `pipeline/data/metadata/<flavor>.json` |
| Images | `readie-pipeline-<flavor>`, `readie-worker-base-<flavor>`, `readie-worker-<flavor>` |

The router reads the catalogue to choose a checkpoint for each request. The compose files mount `catalogues/` read-only, so restart the router after replacing a catalogue. The `make clean-artifacts` target removes the manifest and checkpoints for a flavor.

## Verify

After `make worker-base`, confirm that the printed checkpoint count is greater than zero. After starting the stack, confirm that the worker log reports the checkpoints:

```bash
docker compose logs worker
```

The log contains an `artifacts loaded` line that shows the number of checkpoints. `checkpoints=0` means every call cold-starts. See [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack#restore-or-cold-start).

## Worker behavior with missing artifacts

The worker looks for all artifacts under `/var/lib/readie` inside its image. The worker handles missing pieces as follows:

- No root filesystem: the worker starts and registers with the router with an error status, so the router places no work on it. Every request fails and names the missing artifact.
- Root filesystem but no manifest or checkpoints: the worker serves only cold starts.
- Checkpoints but no root filesystem: the worker refuses to start.
- A checkpoint that does not match the worker (different sandbox spec or gVisor version): the worker logs the reason and drops all checkpoints, serving cold starts. If `CHECKPOINT_STRICT_COMPAT` is `false`, the worker keeps the checkpoints and logs a warning instead.

## Troubleshoot

### The capture step fails immediately

Confirm that the host is amd64, that Docker can run privileged containers, and that a TTY is attached.

### Restores log "falling back to a cold start"

The worker could not restore a checkpoint. Check the worker log for the reason, and see [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack).

### Checkpoints appear to be ignored

A new gVisor or executor version requires new checkpoints. Rebuild with `make generation`.

## What's next

- [Run the stack locally](/docs/contributing/run-the-stack)
- [Configure the services](/docs/contributing/configure-services)
- [Make targets](/docs/contributing/make-targets)
