# readie-pipeline

The pipeline is the offline half of Readie. It analyzes the request corpus,
decides which package sets are worth pre-importing, and captures one gVisor
checkpoint per set. The output is a generation: the executor root filesystem
plus every checkpoint taken against it.

The two ship together because a gVisor checkpoint restores only into the
filesystem it was captured from. A checkpoint without its rootfs is unusable.

For the conceptual background, see [Building
checkpoints](https://readie.org/docs/architecture/building-checkpoints). For the
step-by-step procedure, see [Build
checkpoints](https://readie.org/docs/contributing/build-checkpoints).

The pipeline requires Python 3.12 or later (`requires-python = ">=3.12"` in
`pyproject.toml`). CI and `.python-version` use Python 3.13.

```sh
make install   # sync the virtualenv from the lockfile
make test      # planner, models and parser tests; no runsc needed
make lint type # ruff + mypy --strict
```

## The stages

```sh
readie-pipeline corpus    # generate request snippets with a hosted model
readie-pipeline analyze   # measure every package installed here, and their deps
readie-pipeline plan      # choose checkpoint contents, write the OCI spec
readie-pipeline build     # capture one gVisor checkpoint per planned set
readie-pipeline capture   # plan and then build, in one process
```

Each stage runs on its own. `plan` needs only the committed data (no runsc, no
network, no container), so it is the stage to iterate on.

### analyze

`analyze` measures whatever is importable in the environment it runs in. That
environment is the base image, not the corpus's imports. The stage must run
natively inside the base image, not alongside a copy of it. Mixing the tool's
own libc with a foreign one crashes with SIGFPE as soon as anything beyond pure
Python is involved, and `import pandas` is enough to trigger it.

`make analyze FLAVOR=cpu|gpu` (root `Makefile`) builds the `analyzer` target of
`pipeline/Dockerfile` and runs it. That target installs `readie-pipeline` with
`--no-deps` directly on top of the base image, so only one libc is present. The
result is written on the host to `data/metadata/<flavor>.json`.

`plan` and `capture` read whatever `data/metadata/<flavor>.json` already
contains. They do not regenerate it.

### capture

`capture` is the image's default command. It exists because `plan` and `build`
must share a container. `plan` writes the bundle's `config.json` under
`$BASE_DIR`, which lives inside the image and is not mounted out, so a second
`docker run` would start from a bundle with no spec in it.

Planning at image build time does not work either. The capture bind-mounts a
host directory over `$EXECUTOR_DIR`, and a bind mount hides whatever the image
wrote there.

### build

`build` removes everything inside `$EXECUTOR_DIR` (the output directory) before
it reads the plan. The directory itself is kept.

> **Caution:** When `READIE_PLAN_DIR` is unset, the plan (`checkpoints.json`)
> and the spec fingerprint (`spec-fingerprint.txt`) are written into the output directory.
> Running `build` on its own then deletes them, and the stage fails when it reads
> the plan. Set `READIE_PLAN_DIR` to a separate directory to run `plan` and
> `build` as separate steps, or use `capture`, which runs both in one process.
> The image sets `READIE_PLAN_DIR=/app/plan`.

## What a plan is

```json
[
    {
        "imports": ["pandas", "numpy"],
        "datasets": [],
        "models": [],
        "tokenizers": []
    }
]
```

A plan has one entry per checkpoint. `plan` chooses the entries from all corpus
requests, weighted by the measured import time and disk size of the base image's
packages. See [`planning/`](src/readie_pipeline/planning/) for the algorithm.

### Planners

`READIE_PLANNER` selects the planner. The default, `greedy`, performs greedy
facility selection: it repeatedly adds the package set with the highest marginal
value, so the first N selections are already the best N. The `fixed` planner returns a
supplied list of sets, `pandas` and `numpy` by default, and is meant for
reproducibility runs and tests.

Two bounds apply to the greedy planner:

- `READIE_MAX_CHECKPOINTS` (`--max-checkpoints`) keeps only the first N
  selections.
- `READIE_SIZE_BUDGET_MB` (`--size-budget-mb`) is a total budget across all
  selected checkpoints, not a limit on each one. The default is 2048 MB.

The `--size-budget-mb` help text in `cli.py` reads "per-checkpoint size budget".
That text is inaccurate: `GreedyPlanner.plan` documents the budget as bounding
the total size across every checkpoint selected.

### imports

`imports` lists what the executor pre-imports. It contains only the packages
that some request actually asked for, not their full dependency closure. The
planner still scores a dependency's load time and disk size, because a package's
measured import time assumes that its dependencies are already resident, so a
checkpoint must carry them to deliver that saving. The executor does not need
the dependencies spelled out, because `import pandas` imports numpy as a side
effect.

The catalogue (see [What it writes](#what-it-writes)) reconstructs the closure
from this list when it needs the full resident set.

## How a checkpoint is captured

`plan` generates the bundle's `config.json` by calling `ocispec`, the worker's
own spec builder, which is compiled into this image. Using one generator keeps
the sandbox that the pipeline captures and the sandbox that the worker restores
into the same shape.

For each planned set, `build` then does the following:

1. Rewrites only the sandbox's `READIE_PREIMPORT` environment variable.
2. Starts the sandbox with `runsc run`. The spec carries the annotations
   `dev.gvisor.internal.checkpoint.enable` and
   `dev.gvisor.internal.checkpoint.path`.
3. Waits while the executor pre-imports the packages and then triggers the
   checkpoint itself by writing to `/proc/gvisor/checkpoint`. The pipeline does
   not call `runsc checkpoint`.
4. Restores the checkpoint once with `runsc restore` to measure its restore
   time.

Varying the environment rather than the executor's source keeps the build safe
to interrupt, because the shared rootfs is never modified. It works because
`runsc.Fingerprint` deliberately excludes `process.env`. Every checkpoint
therefore shares one fingerprint and restores into the worker's sandbox. A test
on the Go side pins this behavior.

`build` always captures an additional empty checkpoint, `checkpoint_0`, with
nothing pre-imported. After capture, `build` fits a line to restore time
against checkpoint size across all checkpoints. The slope is the measured
alpha, and `build` prints it next to the planned `READIE_ALPHA` value together
with the drift between the two. The measured value is the one written to the
catalogue.

## Layout

| Path                              | Contents                                    |
| --------------------------------- | ------------------------------------------- |
| `$BASE_DIR`                       | the OCI bundle: `config.json` + `rootfs/`   |
| `$EXECUTOR_DIR`                   | host-side outputs; mount this out           |
| `$EXECUTOR_DIR/checkpoints/<id>/` | one checkpoint image plus its `meta.json`   |
| `$EXECUTOR_DIR/manifest.json`     | what those checkpoints can be restored into |
| `$EXECUTOR_DIR/catalogue.json`    | the catalogue for the router                |
| `$READIE_PLAN_DIR`                | the plan and the spec fingerprint           |
| `data/`                           | the committed corpus and package metadata   |

`$EXECUTOR_DIR` here is a host directory. It is distinct from the sandbox's own
`EXECUTOR_DIR`, which is `/tmp`, where the executor binds its socket.

`$READIE_PLAN_DIR` is separate because `$EXECUTOR_DIR` is copied into the base
image wholesale. The plan and the fingerprint are inputs to a capture, not
artifacts that a worker ships beside its manifest. When `READIE_PLAN_DIR` is
unset, they are written with the outputs, which suits a local `plan` run.
See the caution under [build](#build).

## Build and run

[`Dockerfile`](Dockerfile) defines everything a checkpoint is bound to, plus the
`analyze` stage, in three targets:

| Target        | Contents                                                              |
| ------------- | --------------------------------------------------------------------- |
| `analyzer`    | `readie-pipeline` on top of the base image itself, nothing else       |
| `pipeline`    | this tool: runsc, the planner, the rootfs as an OCI bundle, `ocispec` |
| `worker-base` | what a worker runs on: runsc, the rootfs, the captured checkpoints    |

A gVisor checkpoint restores only into the filesystem it was captured from,
through the runsc release that captured it. Both therefore live in this
Dockerfile, and the gVisor release is pinned once for capture and restore. The
worker's own image is `worker/Dockerfile`, which is `FROM readie-worker-base`
and adds only the Go server.

From the repository root, one command captures checkpoints, bakes them into the
base, and builds a worker on it:

```sh
make generation                     # tags readie-worker:<timestamp> and :latest
make generation TAG=my-experiment
```

Capture is a separate phase because `docker build` cannot capture a checkpoint.
runsc needs privileged namespace access, which a stock BuildKit builder does not
grant. The pipeline therefore runs as a privileged container, and `worker-base`
is built around what it wrote.

The generation consists of three steps that can also run separately:
`make capture`, `make worker-base` and `make worker-image`. Running
`make worker-image` alone redeploys a worker code change, because the base
already holds the checkpoints. `make clean-artifacts` discards what was
captured.

To iterate on planning alone, without gVisor, network, or a container:

```sh
make -C pipeline test
docker run --rm readie-pipeline plan --no-spec --max-checkpoints 3
```

## What it writes

```text
pipeline/out/<flavor>/
├── manifest.json           runsc_version, spec_fingerprint, executor_argv,
│                           executor_protocol, python_path, overlay, network
├── catalogue.json          measured alpha, item costs, per-checkpoint contents
└── checkpoints/<id>/       runsc image + meta.json
```

The `manifest.json` and `checkpoints/` layout is the shape that
`worker/internal/artifact` reads, so nothing is reshaped between capturing a
checkpoint and shipping it.

`catalogue.json` is what the router reads to select a checkpoint. It records
the alpha measured during `build`, every measured item's cost, and each
checkpoint's contents and precomputed size term. `make capture` copies it to
`catalogues/<flavor>.json`, which the router mounts as `CATALOGUE_DIR`. The
shared `alpha` constant must match between `READIE_ALPHA` here and `ALPHA` in
the router. See [Cost
model](https://readie.org/docs/concepts/cost-model).

Neither the manifest nor the checkpoint metadata carries a `rootfs_id`. A
generation has one rootfs, baked in beside its checkpoints, so there is nothing
to identify and the pairing is correct by construction. Identity is the base
image tag. The manifest has no id of its own, because a second name for the same
build could disagree with the first.

## Configuration

| Variable                                                 | Purpose                                                                             |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| `BASE_DIR`, `EXECUTOR_DIR`                               | the bundle and the output directory                                                 |
| `READIE_PLAN_DIR`                                        | where the plan goes, if not with the outputs                                        |
| `ROOTFS_PYTHONPATH`                                      | must correspond to the rootfs stage                                                 |
| `SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY` | must match the worker's                                                             |
| `READIE_PLANNER`                                         | `greedy` or `fixed`                                                                 |
| `READIE_MAX_CHECKPOINTS`, `READIE_SIZE_BUDGET_MB`        | planner bounds; the size budget is a total across all checkpoints                   |
| `READIE_ALPHA`                                           | size-versus-time weight used by the planner; `build` records the measured value     |
| `FLAVOR`                                                 | `cpu` or `gpu`; the generation this run produces                                    |
| `READIE_DATA_DIR`                                        | overrides the committed corpus location                                             |
| `RUNSC_BINARY`, `OCISPEC_BINARY`                         | the runtime and the OCI-spec generator (defaults `runsc`, `/usr/local/bin/ocispec`) |
| `READIE_CHECKPOINT_TIMEOUT`                              | how long a sandbox may take to checkpoint before `build` gives up (default `300`)   |
| `AZURE_ENDPOINT`, `AZURE_API_KEY`, `AZURE_MODEL_NAME`    | `corpus` only                                                                       |
| `AZURE_API_VERSION`                                      | `corpus` only; default `2025-03-01-preview`                                         |

## Known gaps

- `datasets`, `tokenizers` and `models` are carried through the schema and the
  plan, but only packages are pre-imported. Loading a model into the captured
  process is a much larger change, because it alters what a checkpoint costs to
  store.
- The rootfs is the full base image regardless of what the plan selects. This is
  deliberate: a new request distribution then regenerates only checkpoints, not
  tens of gigabytes of filesystem. A sandbox does carry far more than any one
  checkpoint needs.
