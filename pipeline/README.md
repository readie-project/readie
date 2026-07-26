# crfs-pipeline

The offline half. It analyses the request corpus, decides which package sets are
worth pre-importing, and captures one gVisor checkpoint per set. The output is a
**generation**: the executor root filesystem plus every checkpoint taken against
it.

The two ship together because a gVisor checkpoint only restores into the
filesystem it was captured from. A checkpoint that travels without its rootfs is
unusable.

```sh
make install   # sync the virtualenv from the lockfile
make test      # planner, models and parser tests; no runsc needed
make lint type # ruff + mypy --strict
```

## The stages

```sh
crfs-pipeline corpus    # generate request snippets with a hosted model
crfs-pipeline analyze   # measure package sizes and import times
crfs-pipeline plan      # choose checkpoint contents, write the OCI spec
crfs-pipeline build     # capture one gVisor checkpoint per planned set
```

Each runs alone. `plan` needs only the committed data — no runsc, no network, no
container — so it is the stage to iterate on.

The previous `setup.py` and `main.py` were module-scope scripts that ran on
*import*, so neither could be tested and neither stage could be run without the
other.

## What a plan is

```json
[{"imports": ["pandas", "numpy"], "datasets": [], "models": [], "tokenizers": []}]
```

One entry per checkpoint. `plan` chooses them from 9,773 corpus requests over 88
top-level packages, weighted by 918 packages' measured import time and disk
size. See [`planning/`](src/crfs_pipeline/planning/) for the algorithm and why
it is greedy.

## How a checkpoint is captured

`plan` generates the bundle's `config.json` by calling `ocispec` — the worker's
own spec builder, compiled into this image. That indirection is the point: the
sandbox this pipeline captures and the one the worker restores into must have
the same shape, and two independent generators cannot be kept in agreement by
review.

`build` then, for each planned set, rewrites only the sandbox's `CRFS_PREIMPORT`
environment variable, runs the sandbox, waits for the executor to print
`READY_FOR_CHECKPOINT`, and captures the process mid-sleep.

Varying the environment rather than the executor's source is what makes this
safe to interrupt. The previous implementation prepended `import` lines to the
executor *inside the shared rootfs* and restored the file in a `finally`; a
build killed in between left that tree mutated for the next run. It also works
only because `runsc.Fingerprint` excludes `process.env` deliberately, so every
checkpoint in a generation shares one fingerprint and all of them restore into
the worker's sandbox — pinned by a test on the Go side.

## Layout

| Path | |
|---|---|
| `$BASE_DIR` (`/app/executorfs`) | the OCI bundle: `config.json` + `rootfs/` |
| `$EXECUTOR_DIR` (`/app/executor`) | host-side outputs; mount this out |
| `$EXECUTOR_DIR/checkpoints/<id>/` | one checkpoint image plus its `meta.json` |
| `$EXECUTOR_DIR/generation.json` | the manifest binding those checkpoints to a rootfs |
| `data/` | the committed corpus and package metadata |

`$EXECUTOR_DIR` here is a *host* directory and is deliberately distinct from the
sandbox's own `EXECUTOR_DIR`, which is `/tmp` — where the executor binds its
socket.

## Build and run

The rootfs is defined once, in `../rootfs/Dockerfile`, and consumed by both this
pipeline and the worker. Build it first:

```sh
cd ..
docker build -t crfs-executor-rootfs:latest -f rootfs/Dockerfile .
docker build -t crfs-pipeline -f pipeline/Dockerfile .
```

gVisor needs to create namespaces and install seccomp filters, which the default
profiles block:

```sh
mkdir -p out
docker run --rm \
  --privileged \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  -e GENERATION_ID=gen-$(date -u +%Y%m%d-%H%M%S) \
  -e ROOTFS_ID=$(docker inspect --format '{{index .RepoDigests 0}}' crfs-executor-rootfs:latest) \
  -v "$PWD/out:/app/executor" \
  crfs-pipeline
```

`ROOTFS_ID` is the identity the worker checks a checkpoint against, so it should
be the image digest rather than a tag.

## Install a generation on a worker

```sh
GEN=<generation id from the run output>
DEST=/var/lib/crfs/generations/$GEN

mkdir -p "$DEST/rootfs"
cp out/generation.json "$DEST/"
cp -r out/checkpoints  "$DEST/"

docker create --name rootfs-export crfs-executor-rootfs:latest
docker export rootfs-export | tar -x -C "$DEST/rootfs"
docker rm rootfs-export
```

The worker scans `$ARTIFACT_ROOT/generations/` at startup and can hold several at
once, so installing a new one alongside the old lets warm sandboxes finish on the
filesystem they started with.

## Configuration

| | |
|---|---|
| `BASE_DIR`, `EXECUTOR_DIR` | the bundle and the output directory |
| `GENERATION_ID`, `ROOTFS_ID` | generation identity; `ROOTFS_ID` should be an image digest |
| `ROOTFS_PYTHONPATH` | must correspond to the rootfs image |
| `SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY` | must match the worker's |
| `CRFS_PLANNER` | `greedy` or `fixed` |
| `CRFS_MAX_CHECKPOINTS`, `CRFS_SIZE_BUDGET_MB` | planner bounds |
| `CRFS_DATA_DIR` | overrides the committed corpus location |
| `AZURE_ENDPOINT`, `AZURE_API_KEY`, `AZURE_MODEL_NAME` | `corpus` only |

## Known gaps

- `datasets`, `tokenizers` and `models` are carried through the schema and the
  plan, but only packages are pre-imported. Loading a model into the captured
  process is the obvious next step and a much larger one: it changes what a
  checkpoint costs to store.
- The rootfs is the full base image regardless of what the plan selects. That is
  deliberate — a new request distribution then regenerates only checkpoints, not
  tens of gigabytes of filesystem — but a sandbox does carry far more than any
  one checkpoint needs.
