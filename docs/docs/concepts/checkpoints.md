---
title: Checkpoints
sidebar_position: 2
description: The contents of a Readie checkpoint, how checkpoints are stored, and how the router selects one for each call.
---

A checkpoint is a saved copy of a running Python process that has already imported a chosen set of packages. The worker restores it into a new sandbox, which is much faster than starting Python and importing the same packages. Readie builds several checkpoints ahead of time and selects one for each call.

## Purpose

Importing a large package can take seconds. `import torch` or `import pandas` loads many files and runs a large amount of start-up code. If every call started a fresh Python process, most of the call time would go to imports. This is a [cold start](/docs/concepts/cold-starts-and-restore).

A checkpoint removes that work from the call. The import happens once, offline, and the result is saved. At call time Readie loads the saved process into memory, and the packages are already present.

## Contents of a checkpoint

Each checkpoint is a snapshot of one [sandbox](/docs/concepts/sandboxes). The sandbox holds a Python process that runs the Readie executor, the program that later runs the function. Before the snapshot, the executor imports a list of modules. The sandbox runtime, gVisor's `runsc`, then writes the whole process state to disk, including memory and open files.

The executor follows this order:

1. It imports the chosen modules.
2. It asks gVisor to take the snapshot.
3. After the snapshot or restore, it opens its listening socket.

The socket is created late on purpose. A socket saved inside a snapshot would refer to a connection that no longer exists after a restore.

## Storage on disk

A set of checkpoints built together is a **generation**. A generation lives in one folder:

```text
manifest.json
checkpoints/
  checkpoint_0/   meta.json + gVisor image files
  checkpoint_1/   meta.json + gVisor image files
  ...
```

The generation is baked into the worker's container image, next to the root filesystem from which the checkpoints were captured. A checkpoint works only with that exact filesystem, so shipping the two together keeps them in step.

`manifest.json` describes the whole generation:

- the `runsc` version that captured the checkpoints
- a fingerprint (a hash) of the sandbox shape: command, mounts, namespaces, CPU and process limits, and the overlay, network, and GPU modes
- the command the sandbox runs
- the executor wire-protocol version
- the Python path inside the sandbox
- the overlay and network settings, and a creation time

`meta.json` in each checkpoint folder records the checkpoint ID, the `runsc` version, the same fingerprint, and the lists of imports, datasets, models, and tokenizers that the checkpoint holds.

## Checkpoint IDs

IDs are plain names such as `checkpoint_0` and `checkpoint_1`. They are unique within a worker.

`checkpoint_0` is the empty checkpoint, with nothing pre-imported. Every other checkpoint pre-imports a group of packages that calls tend to use together. The default pipeline settings in the repository Makefile allow up to 15 of these, so a full generation has up to 16 checkpoints.

## Compatibility check at worker start-up

A snapshot restores only into a sandbox of the same shape. When a worker starts, it computes its own fingerprint, reads its own `runsc` version, and compares both with the manifest.

If they differ, the default strict behavior drops all checkpoints and serves cold starts only, with an error in the log. Setting `CHECKPOINT_STRICT_COMPAT=false` keeps the checkpoints and logs a warning instead, but restores can then fall back to cold starts. A typical cause of a mismatch is a worker whose sandbox settings (`SANDBOX_NETWORK`, `SANDBOX_OVERLAY`, `SANDBOX_GPU`) differ from the settings used when the checkpoints were built.

## Selection for a call

The router makes the selection. It does not look inside checkpoints. It reads a small summary file, the [catalogue](/docs/concepts/flavors-and-catalogues), that the pipeline writes next to the checkpoints.

1. The client scans the function source and sends the module names it uses, for example `numpy` and `pandas`.
2. The router [selects a worker](/docs/architecture/placement).
3. For a new container, the router scores every checkpoint in the catalogue of that worker. The score trades the size of the checkpoint against the import time it saves. See [Cost model](/docs/concepts/cost-model).
4. The router sends the ID of the winning checkpoint to the worker.
5. The worker restores that checkpoint. If the checkpoint is missing or the restore fails, the worker logs a warning and starts cold. The response reports which checkpoint was used.

If a call belongs to a [session](/docs/concepts/sessions) that already has a warm container, no checkpoint is selected and the call returns to that container.

A function can opt out with `@remote(disable_optimized_execution=True)`. The router then skips checkpoints and cold-starts. If the container of the session was already restored from a checkpoint, the router rejects the request with a precondition error instead of ignoring the flag.

## Origin of checkpoints

The offline pipeline builds checkpoints. It decides which packages go into which checkpoint, runs each one in a sandbox, and captures it. Capture requires a gVisor host on amd64.

## What's next

- [Cost model](/docs/concepts/cost-model)
- [Flavors and catalogues](/docs/concepts/flavors-and-catalogues)
- [Building checkpoints](/docs/architecture/building-checkpoints)
- [Build checkpoints](/docs/contributing/build-checkpoints)
