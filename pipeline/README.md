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
crfs-pipeline capture   # plan and then build, in one process
```

Each runs alone. `plan` needs only the committed data — no runsc, no network, no
container — so it is the stage to iterate on.

`capture` is the image's default command and exists because those two stages have
to share a container: `plan` writes the bundle's `config.json` under `$BASE_DIR`,
which lives inside the image and is not mounted out, so a second `docker run`
would start from a bundle with no spec in it. Planning at *build* time does not
work either — the capture bind-mounts a host directory over `$EXECUTOR_DIR`, and
a bind mount hides whatever the image wrote there.

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
checkpoint shares one fingerprint and all of them restore into the worker's
sandbox — pinned by a test on the Go side.

## Layout

| Path | |
|---|---|
| `$BASE_DIR` | the OCI bundle: `config.json` + `rootfs/` |
| `$EXECUTOR_DIR` | host-side outputs; mount this out |
| `$EXECUTOR_DIR/checkpoints/<id>/` | one checkpoint image plus its `meta.json` |
| `$EXECUTOR_DIR/manifest.json` | what those checkpoints can be restored into |
| `$CRFS_PLAN_DIR` | the plan and the spec fingerprint |
| `data/` | the committed corpus and package metadata |

`$EXECUTOR_DIR` here is a *host* directory and is deliberately distinct from the
sandbox's own `EXECUTOR_DIR`, which is `/tmp` — where the executor binds its
socket.

`$CRFS_PLAN_DIR` is separate from it because `$EXECUTOR_DIR` is copied into the
base image wholesale. The plan and the fingerprint are inputs to a capture, not
artifacts a worker ships beside its manifest. Unset, they go with the outputs,
which is what a local `plan` run wants.

## Build and run

[`Dockerfile`](Dockerfile) defines everything a checkpoint is bound to, in two
targets:

| Target | |
|---|---|
| `pipeline` | this tool: runsc, the planner, the rootfs as an OCI bundle, `ocispec` |
| `worker-base` | what a worker runs on: runsc, the rootfs, the captured checkpoints |

A gVisor checkpoint only restores into the filesystem it was captured from,
through the runsc release that captured it — so both live here, and the gVisor
release is pinned once for the capture and the restore alike. The worker's own
image is `worker/Dockerfile`, which is `FROM crfs-worker-base` and adds only the Go
server.

From the repository root, one command captures checkpoints, bakes them into the
base, and builds a worker on it:

```sh
make generation                     # tags crfs-worker:<timestamp> and :latest
make generation TAG=my-experiment
```

Capture is its own phase because `docker build` cannot capture a checkpoint: runsc
needs privileged namespace access, which a stock BuildKit builder will not grant.
So this pipeline runs as a privileged container, and `worker-base` is built around
what it wrote.

`make capture`, `make worker-base` and `make worker-image` are the three steps if
you want them separately — and `make worker-image` alone is how a worker code
change is redeployed, since the base already holds the checkpoints.
`make clean-artifacts` discards what was captured.

To iterate on planning alone — no gVisor, no network, no container:

```sh
make -C pipeline test
docker run --rm crfs-pipeline plan --no-spec --max-checkpoints 3
```

## What it writes

```
pipeline/out/
├── manifest.json           runsc_version, spec_fingerprint, executor_argv,
│                           executor_protocol, python_path, overlay, network
└── checkpoints/<id>/       runsc image + meta.json
```

Exactly the shape `worker/internal/artifact` reads, so nothing is reshaped
between capturing a checkpoint and shipping it. Neither file carries a
`rootfs_id`: there is one rootfs, baked in beside these checkpoints, so there is
nothing to identify — the pairing is correct by construction rather than by a
field nobody compared.

Identity is the base image tag. The manifest has no id of its own, which would be
a second name for the same build, free to disagree.

## Configuration

| | |
|---|---|
| `BASE_DIR`, `EXECUTOR_DIR` | the bundle and the output directory |
| `CRFS_PLAN_DIR` | where the plan goes, if not with the outputs |
| `ROOTFS_PYTHONPATH` | must correspond to the rootfs stage |
| `SANDBOX_NETWORK`, `SANDBOX_HOST_UDS`, `SANDBOX_OVERLAY` | must match the worker's |
| `CRFS_PLANNER` | `greedy` or `fixed` |
| `CRFS_MAX_CHECKPOINTS`, `CRFS_SIZE_BUDGET_MB` | planner bounds |
| `CRFS_ALPHA` | size-vs-time weight |
| `FLAVOR` | `cpu` or `gpu`; the generation this run produces |
| `CRFS_DATA_DIR` | overrides the committed corpus location |
| `RUNSC_BINARY`, `OCISPEC_BINARY` | the runtime and the OCI-spec generator (defaults `runsc`, `/usr/local/bin/ocispec`) |
| `CRFS_READY_TIMEOUT` | how long a sandbox may take to print `READY_FOR_CHECKPOINT` before `build` gives up (default `300`) |
| `AZURE_ENDPOINT`, `AZURE_API_KEY`, `AZURE_MODEL_NAME` | `corpus` only |
| `AZURE_API_VERSION` | `corpus` only; default `2025-03-01-preview` |

## Known gaps

- `datasets`, `tokenizers` and `models` are carried through the schema and the
  plan, but only packages are pre-imported. Loading a model into the captured
  process is the obvious next step and a much larger one: it changes what a
  checkpoint costs to store.
- The rootfs is the full base image regardless of what the plan selects. That is
  deliberate — a new request distribution then regenerates only checkpoints, not
  tens of gigabytes of filesystem — but a sandbox does carry far more than any
  one checkpoint needs.
