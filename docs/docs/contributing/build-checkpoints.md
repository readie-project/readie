---
title: Build checkpoints
sidebar_position: 8
description: Measure packages, capture checkpoints, and build a worker image that contains them.
---

Use this procedure to build checkpoints and a worker image from them. For the internals of the pipeline, see [Building checkpoints](/docs/architecture/building-checkpoints). The [glossary](/docs/concepts/glossary) defines the terms used here.

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
2. `worker-base` builds one image with runsc and the filesystem. The checkpoints are not in it.
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

The target measures the packages that most corpus requests use first, and the rest alphabetically. Measuring a package also measures everything it loads, so a large subtree that few requests use can dominate the run. To measure such a subtree after all others, pass a space-separated list:

```bash
make analyze ANALYZE_DEFER="tensorflow keras"
```

An interrupted run continues from its side file, `pipeline/data/metadata/<flavor>.json.partial`, and keeps the packages already measured. `<flavor>` is the value of `FLAVOR`. When a finished `<flavor>.json` from the same image and package versions also exists, the run reuses its discovery results for the names that the side file has not measured. It does not reuse that file's timings.

To measure only the names that the finished `<flavor>.json` already contains, and skip the installed packages and the corpus imports, set `ANALYZE_ONLY_KNOWN`:

```bash
make analyze ANALYZE_ONLY_KNOWN=1
```

The step fails when no usable finished `<flavor>.json` exists. The file that the run writes holds the packages of the side file and these names, and it does not hold the names that only the corpus uses.

## Step 2: Capture checkpoints

Run `make capture` to plan and capture the checkpoints:

```bash
make capture FLAVOR=cpu
```

The target builds the pipeline image and runs it as a privileged container. Inside the container, the pipeline plans which packages go into each checkpoint, captures each checkpoint, times a restore of each, and writes the outputs. The target stops with an error if no `manifest.json` appears, which prevents deploying a generation with no checkpoints unintentionally.

The planner settings can be overridden on the command line. The following values are the Makefile values, which differ from the pipeline's own defaults:

| Variable | Makefile value | Meaning |
| --- | --- | --- |
| `READIE_PLANNER` | `greedy` | Planner: `greedy`, `kmedoids`, `toptime`, `jaccard-kmedoids`, `jaccard-agglomerative` or `fixed`. |
| `READIE_MAX_CHECKPOINTS` | `15` | Maximum checkpoints in a plan. The empty checkpoint is added on top. |
| `READIE_SIZE_BUDGET_MB` | `2048.0` | Total memory budget across all checkpoints. |
| `READIE_ALPHA` | set in the Makefile | Seconds per MB that the planner charges for size. |

For example, to override the checkpoint count and the size weight:

```bash
make capture READIE_MAX_CHECKPOINTS=10 READIE_ALPHA=<seconds-per-mb>
```

## Step 3: Build the worker base image

Run `make worker-base` to build the base image with gVisor and the filesystem. The image does not contain the checkpoints, so this step does not depend on the capture:

```bash
make worker-base FLAVOR=cpu
```

The target builds `readie-worker-base-cpu:latest`. The step is slow because of the large filesystem layer. The compose file mounts `pipeline/out/<flavor>/checkpoints` and `manifest.json` into the worker, so run `docker compose up -d` again after a new capture. The worker reads them once at startup.

## Step 4: Build the worker image

Run `make worker-image` to add the Go worker on top of the base image:

```bash
make worker-image FLAVOR=cpu
```

The target builds `readie-worker-cpu:<timestamp>` and `readie-worker-cpu:latest` from `worker/`. The step takes seconds because it adds only the Go server. If the base image is missing, the target builds it first. To name the tag, set `TAG=<tag-name>`.

After a change to worker code only, run this step alone. It does not capture checkpoints again.

## Use a minimal root filesystem

By default the root filesystem is the base image plus the executor. A base image that carries no packages, such as `python:3.12-alpine`, can instead install only the distributions that the plan needs. This works with either planner.

1. Write the list of distributions from the plan. The target runs on the host and needs `uv`:

   ```bash
   make rootfs-requirements PLAN_DIR=experiments/a/plan
   ```

2. Build the generation from that list and the minimal base:

   ```bash
   make generation \
     ROOTFS_BASE_IMAGE=python:3.12-alpine \
     ROOTFS_PYTHONPATH=/usr/local/lib/python3.12/site-packages \
     ROOTFS_REQUIREMENTS=experiments/a/plan/rootfs-requirements.txt \
     REUSE_PLAN_DIR=experiments/a/plan
   ```

A distribution can install and still fail to import because the base lacks a system library. The failure appears when a checkpoint is captured, as an `ImportError` from the preimport. Pass the Debian packages that provide the libraries with `ROOTFS_APT_PACKAGES`, a comma-separated list. A base without `apt-get` fails the build when the list is not empty.

Each line of the list pins a distribution to the version the metadata recorded from the Kaggle image, such as `numpy==2.0.2`, so the filesystem gets the packages whose timings and sizes the plan was built from. The variable takes several indexes separated by commas. A distribution with no recorded version is a bare name. Every install also gets the whole list as constraints, so a later package's dependencies cannot move an earlier pin. The image build installs each one separately and keeps going when one fails. The failures are printed in the build log and written to `/etc/readie-rootfs-skipped.txt` in the root filesystem. With `REUSE_PLAN_DIR`, the capture step also copies that list to `rootfs-skipped.txt` in the plan directory and prints a warning. A plan that imports a skipped distribution fails at that checkpoint with `ModuleNotFoundError`, so read this file when a capture stops on a missing module. Set `ROOTFS_REQUIREMENTS_STRICT=true` to fail the build instead. The catalogue lists only the distributions that installed.

Packages must be installed by the image build, not by the capture step. The worker image copies the root filesystem from the image build, so a package installed later, inside the capture container, would not ship with the checkpoints. `REUSE_PLAN_DIR` makes the capture step use the plan from the first command instead of planning again. Without it, the capture step plans inside the container, so pass the same planner settings to both commands.

## Output locations

| Output | Location |
| --- | --- |
| Checkpoints, `manifest.json`, `catalogue.json` | `pipeline/out/<flavor>/` (ignored by git) |
| Router catalogue | `catalogues/<flavor>.json`, copied from the pipeline output |
| Package measurements | `pipeline/data/metadata/<flavor>.json` |
| Images | `readie-pipeline-<flavor>`, `readie-worker-base-<flavor>`, `readie-worker-<flavor>` |

The router reads the catalogue to choose a checkpoint for each request. The compose files mount `catalogues/` read-only, so restart the router after replacing a catalogue. The `make clean-artifacts` target removes the manifest and checkpoints for a flavor.

## Verify

After `make capture`, confirm that the target printed the captured checkpoint names. After starting the stack, confirm that the worker log reports the checkpoints:

```bash
docker compose logs worker
```

The log contains an `artifacts loaded` line that shows the number of checkpoints. `checkpoints=0` means every call cold-starts. See [Troubleshoot the stack](/docs/contributing/troubleshoot-the-stack#restore-or-cold-start).

## Worker behavior with missing artifacts

The worker looks for all artifacts under `/var/lib/readie`: the root filesystem comes from its image, and the manifest and checkpoints are mounted. The compose mounts do not create a missing host path, so `docker compose up` fails when there is no generation. The worker handles missing pieces as follows:

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
