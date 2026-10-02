---
title: Flavors and catalogues
sidebar_position: 6
description: The CPU and GPU worker flavors, and the catalogue file from which the router selects a checkpoint.
---

A **flavor** describes the type of machine a worker is: CPU only, or CPU plus GPU. Each flavor has its own set of checkpoints and its own catalogue, a JSON file that lists the contents of each checkpoint and the import cost of each package. The pipeline writes the catalogue, and the router reads it to select a checkpoint for each call.

## Flavors

Readie provides two flavors, `cpu` and `gpu`:

- A **cpu worker** runs sandboxes without GPU access.
- A **gpu worker** runs sandboxes with `nvproxy` from gVisor turned on, which passes the NVIDIA GPUs of the host through. It is built from a different root filesystem and has its own checkpoints, because a GPU checkpoint must never be restored into a CPU sandbox.

The flavor of a worker comes from the `WORKER_FLAVOR` setting. The default is `cpu`, and only `cpu` and `gpu` are accepted. The worker reports its flavor to the router with every status update. A gpu worker also reports the amount of GPU memory it offers (`WORKER_GPU_TOTAL`).

The pipeline runs once per flavor. `make generation FLAVOR=cpu` and `make generation FLAVOR=gpu` produce independent sets of checkpoints, so the checkpoints of one flavor are never mixed with those of the other. Building for a GPU requires a GPU host. See [Build checkpoints](/docs/contributing/build-checkpoints).

### GPU calls

The router treats a call as a GPU call if the function passes `gpu=True` to `@remote`, or sets any GPU memory budget (`gpu_memory` or `max_gpu_memory`).

- A GPU call can run only on a gpu worker.
- A CPU call prefers a cpu worker. If no cpu worker can take it, the call can run on a gpu worker.

For details, see [Placement](/docs/architecture/placement).

## Catalogues

A catalogue is a JSON file, one per flavor. It is the only input the router uses to select a checkpoint. Without a catalogue, the router sends no checkpoint ID and every container cold-starts.

### Producer and consumer

The pipeline writes the catalogue and the router reads it:

- **Written by the pipeline.** At the end of a build, the pipeline writes `catalogue.json` next to the checkpoints, once per generation. `make capture` copies it to `catalogues/<flavor>.json` in the repository root.
- **Read by the router.** At start-up, the router loads every `*.json` file in the directory named by `CATALOGUE_DIR`. The provided `docker-compose.yml` mounts `./catalogues` read-only at `/var/lib/readie/catalogues` and sets `CATALOGUE_DIR` to match. The router loads catalogues once, so a new file takes effect after a router restart.

The router skips a file that cannot be read, or whose `version` is not `1`, and logs a warning. A bad catalogue never stops the router from starting, and the worst outcome is cold starts. The router files each catalogue under the `flavor` value written inside the JSON, not under the file name.

### Format

The file is large. The committed CPU catalogue is about 400 MB and describes nearly 5,000 packages. It has five top-level keys:

| Key | Meaning |
| --- | --- |
| `version` | Format version. The router accepts only `1`. |
| `flavor` | `cpu` or `gpu`. |
| `alpha` | The weight, in seconds per MB, used to price checkpoint size. See [Cost model](/docs/concepts/cost-model). |
| `items` | Every measured item, keyed by name. |
| `checkpoints` | The checkpoints of this generation. |

Each entry in `items` has these fields:

- `load_time`: seconds to import the item, measured by the pipeline
- `size_mb`: memory the item takes once loaded
- `resource_type`: `package`, `dataset`, `model`, or `tokenizer`
- `dependencies` (optional): the other items it pulls in

Packages are keyed by dotted import name, such as `numpy` or `numpy.linalg`. Datasets, models, and tokenizers carry a prefix, such as `model:gpt2`.

Each entry in `checkpoints` has these fields:

- `id`: for example `checkpoint_3`
- `items`: everything loaded in the checkpoint, including dependencies
- `canonical`: only the packages that some example request asked for, which is what the executor was told to import
- `size_mb`: the total size

The following trimmed sketch uses illustrative values:

```json
{
  "version": 1,
  "flavor": "cpu",
  "alpha": 0.0065,
  "items": {
    "numpy": { "load_time": 0.2, "size_mb": 6.5, "resource_type": "package" }
  },
  "checkpoints": [
    { "id": "checkpoint_0", "items": [], "canonical": [], "size_mb": 0 },
    { "id": "checkpoint_1", "items": ["numpy"], "canonical": ["numpy"], "size_mb": 6.5 }
  ]
}
```

## Checkpoint selection

The router selects a checkpoint in four steps:

1. The router selects a worker first. The flavor of the worker decides which catalogue is used. A gpu worker that serves a CPU call therefore restores a GPU checkpoint, because the choice follows the worker and not the call.
2. The router takes the module names that the client sent and adds everything they depend on.
3. The router scores each checkpoint and keeps the cheapest. The cold start is the baseline. If no catalogue exists for the flavor of the worker, no checkpoint is selected.
4. The router sends the checkpoint ID to the worker.

Calls in a [session](/docs/concepts/sessions) that already has a container skip the selection.

## What's next

- [Cost model](/docs/concepts/cost-model), for the scoring formula and a worked example
- [Placement](/docs/architecture/placement)
- [Build checkpoints](/docs/contributing/build-checkpoints)
