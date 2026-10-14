# catalogues

Per-flavor checkpoint catalogues. The router reads these files at startup and uses
them to choose, for each call, the checkpoint that saves the most import time for
its size. This directory holds the output of `make generation` and `make capture`.

## Files

| File | Written by | Read by |
| ---- | ---------- | ------- |
| `<flavor>.json` (for example `cpu.json`) | `make capture FLAVOR=<flavor>`, which copies it from the pipeline's `catalogue.json` | The router, which mounts this directory read-only |

The JSON files in this directory are stored with Git LFS (`catalogues/*` in
`.gitattributes`), because a catalogue is hundreds of megabytes. Run `git lfs pull`
after cloning to fetch them. This README is excluded from LFS. Do not edit the
JSON files by hand: they are machine-written and tied to a specific set of
checkpoints.

## How the router uses a catalogue

The router loads every `*.json` file in the directory named by `CATALOGUE_DIR`. In
the provided Compose setup, `./catalogues` is mounted at
`/var/lib/readie/catalogues` and `CATALOGUE_DIR` points there.

- A catalogue is keyed by the `flavor` field inside the file, not by the file name.
- A file that is unreadable, is not a JSON object, or has an unsupported `version`
  is skipped with a warning. The router still starts. The current version is 2, so a
  catalogue written before the `distributions` field existed is skipped until it is
  regenerated.
- With no catalogue for a flavor, or no directory at all, every call is a cold
  start. The functions still run, only without a restore.

For a call, the router computes, for each checkpoint, the cost

```text
alpha * size_mb(checkpoint) + sum of load_time for the required items
                              that the checkpoint does not already contain
```

and compares it with a cold start, which has no size term and pays every load
time. It picks the cheapest. A checkpoint is chosen only when it saves more import
time than its size costs. A tie goes to the later checkpoint in the file.

A call can also pin a checkpoint with `checkpoint_id`. The router then skips the
scoring and accepts the id only if the catalogue of the chosen worker lists a
checkpoint with exactly that id. No id is special, `checkpoint_0` included. An id
that is not listed is rejected with `INVALID_ARGUMENT`.

The router also uses the catalogue to trim a call's `packages`. It removes every
entry whose distribution is in the top-level `distributions` list, whatever version
the entry asks for, because the worker's root filesystem already carries it. An
entry with extras is kept, and with no catalogue nothing is removed.

The required items are the packages the function imports plus everything they
depend on (their dependency closure). Each shared dependency is counted once.
Items the catalogue has not measured add the same amount to every option, so they
cannot change the result.

## File format

The current format is `version: 2`. The pipeline writes it in
`pipeline/src/readie_pipeline/catalogue.py`, and the router reads it in
`router/src/readie_router/scheduling/catalogue.py`. Change both together and bump
`version`.

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `version` | integer | Format version. The router skips a file with any other value. |
| `flavor` | string | `cpu` or `gpu`. The router routes a call to the catalogue of the flavor it needs. |
| `distributions` | array | Every distribution installed in the flavor's root filesystem, as a sorted list of canonical names. The pipeline lists it by running `metadata/inventory.py` (the module `analyze` also resolves distributions with) with the root filesystem's own `python`. If that listing fails, the field is an empty list and the router filters nothing. |
| `alpha` | number | Seconds of import time one MB of checkpoint size is worth. Measured by the pipeline after capture. The router applies this value, so the router has no `alpha` setting of its own. A missing value falls back to a built-in default. |
| `items` | object | Every measured item, keyed by name. |
| `checkpoints` | array | The checkpoints of this generation. |

Each entry of `items` has these fields:

| Field | Meaning |
| ----- | ------- |
| `load_time` | Seconds to import the item when its dependencies are already resident. |
| `size_mb` | Memory the item occupies, in MB. Memory is what a restore copies back, not disk size. |
| `resource_type` | `package`, `dataset`, `model` or `tokenizer`. |
| `dependencies` | Item keys the item depends on (packages only). The list is already transitive. |
| `distribution` | The PyPI distribution that provides the item, as the metadata recorded it (the first provider of the top-level name). Empty for `dataset`, `model` and `tokenizer` items. |

Packages are keyed by their dotted import name (`numpy`, `sklearn.svm`). Datasets,
models and tokenizers carry a kind prefix (`model:gpt2`). The names match how the
client lists a function's requirements.

Each entry of `checkpoints` has these fields:

| Field | Meaning |
| ----- | ------- |
| `id` | The checkpoint id, for example `checkpoint_3`. The router sends this id to the worker. |
| `items` | The full dependency closure resident in the checkpoint. Used for pricing. |
| `canonical` | Only the items that some example call asked for. These are the modules the executor imported before the checkpoint. |
| `size_mb` | Total memory size of the checkpoint. |

`checkpoint_0` is always present and empty. It has no items and size 0, so it is
the choice when nothing else saves more than it costs. It is not the same as a
cold start, which uses no checkpoint at all.

A trimmed example (the values are illustrative):

```json
{
  "version": 2,
  "flavor": "cpu",
  "alpha": 0.0065,
  "distributions": ["numpy"],
  "items": {
    "numpy": { "distribution": "numpy", "load_time": 0.2, "size_mb": 6.5, "resource_type": "package" }
  },
  "checkpoints": [
    { "id": "checkpoint_0", "items": [], "canonical": [], "size_mb": 0 },
    { "id": "checkpoint_1", "items": ["numpy"], "canonical": ["numpy"], "size_mb": 40.2 }
  ]
}
```

The pipeline carries over only the measured resources that the root filesystem
provides. A package is kept when the rootfs has a distribution for it, resolved
exactly as `analyze` resolves the metadata's own (the first provider of the
top-level import name, `distribution_for` in `metadata/inventory.py`), so the
metadata may come from a different image than the one the sandbox runs. The two
sides use one function, so a change to that rule applies to both. Checkpoints are still planned from all the metadata, and the
checkpoint `items` and `canonical` lists and each item's `dependencies` are pruned
to what was kept.

## Keeping a catalogue in step with the workers

A catalogue describes one generation of checkpoints, so it must match the
checkpoints baked into the worker image.

- Run `make generation FLAVOR=<flavor>` to build the checkpoints, the worker image
  and the catalogue together. `make capture` copies the new catalogue here.
- Deploy the new worker image and the new catalogue together. If the router names a
  checkpoint the worker does not have, the worker logs
  `cannot resolve the requested checkpoint; starting cold` and runs a cold start.
- Restart the router after replacing a file. It reads the directory once, at startup.
- The pipeline tests can rewrite a committed `cpu.json` as a side effect. Run
  `git status` after `make test` and discard unintended changes.
