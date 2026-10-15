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
make test      # planner, corpus and model tests; no runsc needed
make lint type # ruff + mypy --strict
```

## The stages

```sh
readie-pipeline corpus    # build the workload corpus for FLAVOR (Kaggle, Hugging Face, hosted model)
readie-pipeline analyze   # measure every package installed here, and their deps
readie-pipeline plan      # choose checkpoint contents, write the OCI spec
readie-pipeline build     # capture one gVisor checkpoint per planned set
readie-pipeline capture   # plan and then build, in one process
```

Each stage runs on its own. `plan` needs only the committed data (no runsc, no
network, no container), so it is the stage to iterate on.

### corpus

`corpus` builds `data/datasets/<flavor>.json`, the set of workloads the planner
reasons over. It generates one flavor per run, taken from `FLAVOR`, and a `cpu`
corpus and a `gpu` corpus never share a file.

Each item has four fields, plus a provenance link:

| Field       | Contents                                                                                   |
| ----------- | ------------------------------------------------------------------------------------------ |
| `code`      | A list of strings. One string is a single execution. Several strings are the cells of a notebook, the steps of a debugging session, or another multi-step session. |
| `domain`    | One of the domains in `corpus/domains.py`.                                                 |
| `source`    | `kaggle`, `kaggle_notebook`, `huggingface`, `huggingface_notebook`, `agent_short`, `agent_notebook` or `agent_fix`. |
| `resources` | Strings of the form `<kind>:<value>`. Kinds are `package`, `hf_dataset`, `hf_model`, `hf_tokenizer`, `kaggle_dataset` and `kaggle_model`. |
| `ref`       | The URL fetched code came from; `null` for generated code.                                 |

The first four sources fetch real code through the Kaggle and Hugging Face
APIs. The three `agent_*` sources are written by the hosted model, with a prompt
that depends on the flavor: a `cpu` run asks for work that needs no GPU, and a
`gpu` run asks for CUDA workloads. Fetched code is classified by the same model,
and only code that matches the run's flavor is kept.

Every candidate passes four gates before it is stored. The flavor judgement is
one model call per candidate, so the calls for a batch run together, and a candidate that an earlier gate would reject is never sent:

1. It is not a duplicate. Duplicates are detected by `ref`, by the normalized
   program (formatting and comments do not matter), and by token overlap.
2. Every step parses as Python.
3. It matches the run's flavor.
4. Every resource exists. The `resources` list is derived from the code itself
   (imports, `from_pretrained`, `load_dataset`, `kagglehub` downloads) plus the
   datasets a Kaggle notebook declares. PyPI confirms packages, the Hugging Face
   Hub confirms models, tokenizers and datasets, and Kaggle confirms Kaggle
   datasets and models. A generated item that names a resource that does not
   exist is rewritten once; fetched code is rejected. A lookup that fails for
   another reason (network, rate limit) is never recorded as "does not exist".
   Answers are cached in `data/cache/verified.json`, one file shared by every
   flavor. The cache holds only what PyPI, the Hugging Face Hub and Kaggle said,
   which does not depend on the flavor, so a `gpu` run reuses what a `cpu` run
   already looked up. The base image's package list is flavor specific, so it
   is applied after the cache and never stored in it. When PyPI does not
   recognize a package, that list decides, since a package installed in the
   image is real whatever its PyPI project is called.

An item that passes the gates is then made runnable, so it can be executed in a
sandbox that has no notebook and none of the files it expects. Nothing is
dropped, and what cannot be fixed stays as it was.

- **Mechanical fixes** (`corpus/runnable.py`). IPython syntax that does not parse
  is turned into Python or a comment: `%matplotlib` and similar display magics
  are commented out, `%time` keeps its statement, `%env`/`%cd` become `os` calls,
  `!cmd` becomes `subprocess.run`, `%%bash` and `%%writefile` become code, and
  `!pip install`/`%pip install` lines are commented out and never run. Every
  change keeps the original text in a `# [readie] ...` comment. A cell is only
  touched when it does not parse, and a change that would leave it unparsable is
  discarded. Code that uses `/kaggle/` or `../input` paths gets a short prelude
  (between `# [readie:prelude]` and `# [readie:end]`) that creates
  `/kaggle/working` and `/kaggle/input` and works from the first.
- **Stand-in data** (`corpus/standins.py`). Code that reads a file the item does
  not create (for example `../input/titanic/train.csv`) fails at the first read.
  The model is shown the notebook and rewrites only the cells that read missing
  files, so each file is created first, at the same path, with generated data of
  the same columns, types and rough size (`numpy.random.default_rng(0)`). Nothing
  else in the cell changes. A rewrite is kept only if a static check
  (`corpus/dependencies.py`) finds nothing still reading a missing file, every cell
  parses, and any new import exists. Otherwise the original stays. The code from
  before the rewrite is kept in the item's optional `original` field. Items with
  more than `READIE_CORPUS_REWRITE_MAX_CHARS` of code are not rewritten.
- **Resources are not removed.** A rewritten item still lists its
  `kaggle_dataset:` entries, since they say what the workload is about; only
  imports the rewrite adds are unioned in.

The stand-in data is random, so a rewritten item's results mean nothing, but the
amount of work and the memory it uses stay in the right range. Model downloads
(`from_pretrained`), web requests, environment variables and logins are not
rewritten. The scan only sees literal file names, so a path built from variables
is missed.

Each request to a source also picks a popularity tier (`corpus/popularity.py`),
weighted toward mainstream code. Kaggle requests read the top result pages
ranked by votes for the mainstream tier and deeper pages ranked by recency for
the niche tier. Hugging Face requests rank by downloads near the top of the
listing, or start much further down. The tier only chooses where a search is
entered. A search is then walked: Kaggle pages already read are not read again
(Kaggle ends every search after a fixed number of result pages), a Hugging
Face search is listed once and read on from where earlier requests stopped, and a
request moves to the next page or another phrase, sort or kind when what it found
was already seen. A source therefore reports a shortfall only once its searches
are used up, not after a few unlucky pages. Agent prompts ask for introductory,
everyday or specialised code. The tier keeps the corpus from being either a few
famous tutorials or a collection of curiosities. After a run, the summary lists
the most used packages and how many packages only a single item uses, so the
shape of the distribution is visible.

The search phrases are not fixed. `corpus/domains.py` holds a starting set per
domain, and when a (domain, source) pair stops finding unseen code the source
asks the model for new phrases. The model sees the phrases already tried and the
titles of what was found, and it is told what each site actually matches: Kaggle
matches notebook titles and tags, while the Hugging Face Hub matches repository
names only, so the Hub phrases are short tokens such as library and model names.
Kaggle needs every word of a phrase to match, so the model is asked for single
words and two-word phrases, and longer ones are dropped. A pair keeps widening
for as long as the new phrases find code, and stops after several widenings in a
row that find none. The phrases it learns are
saved in `data/cache/keywords.json` per platform, flavor and domain, and a later
run starts from them. A phrase that finds nothing costs one search and no
failure.

Kaggle search needs every word to match, so a flavor hint appended to a phrase
narrows it sharply. The hint is used for a minority of searches, and a hinted
search that returns nothing is repeated without it.

Kaggle also rate limits, and one budget covers its searches, downloads and
existence checks. A pacer (`corpus/sources/pacing.py`) spaces the calls, doubles
the spacing each time Kaggle answers 429 and eases it back while calls succeed.
A refused call waits and retries with growing delays, for minutes if needed, and
reports any long wait in the progress output, so a throttled
run says so. A call that is still refused after the last attempt is a failed
fetch and counts as one, never as an empty kernel.

Every run appends: the corpus is never overwritten or trimmed, and `--total`
(`READIE_CORPUS_TOTAL`) is how many new items the run adds. They are shared out
over the sources by weight and over the domains, on top of what each pair holds.
The mix is a target number of items per source and domain. Sources are weighted,
real code above generated code by default, and domains are spread evenly. The
schedule is repeatable for a given `READIE_CORPUS_SEED`. The corpus is saved
after every domain and source pair, and at least once a minute while a large
pair is still running, with a progress line (`N of M so far`) as items arrive, so
a long pair shows progress and keeps its items when a run is stopped. Running
the command again adds another `--total` items to what the stopped run saved.
A source that cannot supply its share (for example few notebooks in a domain)
leaves a shortfall that the run reports. Nothing backfills it with generated
code.

```sh
readie-pipeline corpus --clean                            # make an existing corpus runnable, in place
readie-pipeline corpus --clean --no-rewrite               # mechanical fixes only, no model calls
readie-pipeline corpus --output data/evals/cpu.json ...  # generate into another file (an eval set)
readie-pipeline corpus --output ... --exclude data/datasets/cpu.json  # and repeat nothing from that corpus
make pipeline-corpus FLAVOR=gpu ARGS="--dry-run"          # print the planned mix
make pipeline-corpus FLAVOR=gpu ARGS="--total 500"        # append 500 new items
readie-pipeline corpus --source kaggle --domain "Computer Vision"
readie-pipeline corpus --revalidate                       # re-check every resource
```

The stage needs the `corpus` extra (`uv sync --extra corpus`) and credentials,
which are listed in [Configuration](#configuration). It is an authoring step, so
the pipeline image does not include it. `plan` and `analyze` read the result.

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
that some request asked for, not their full dependency closure. The
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
| `data/`                           | the corpus, its lookup cache, and package metadata (stored in Git LFS) |

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
the alpha measured during `build`, every measured item's cost, each
checkpoint's contents and precomputed size term, and the `distributions` the
rootfs provides. `make capture` copies it to
`catalogues/<flavor>.json`, which the router mounts as `CATALOGUE_DIR`.

The metadata can be measured in a different image than the rootfs the sandbox
runs (`analyze` runs in `ROOTFS_BASE_IMAGE`, the rootfs adds the executor's
dependencies), so `build` filters at the end. It runs
`metadata/inventory.py` with the rootfs's own `python`, in a `chroot` with the
sandbox's `PYTHONPATH` (the source goes in on stdin, so nothing is copied into
the rootfs), which lists the installed distributions the way the interpreter
really sees them. `analyze` resolves each package's distribution with the same
module, so a change to that rule applies to both. The catalogue then carries over
only the measured packages that the rootfs has a distribution for. If the rootfs
cannot be listed, `build` prints a `[!]` line and writes the catalogue without
filtering. Planning and capture are unchanged and still use all of the metadata.
The catalogue format is documented in
[`catalogues/README.md`](../catalogues/README.md).

The shared `alpha` constant must match between `READIE_ALPHA` here and `ALPHA` in
the router. See [Cost model](https://readie.org/docs/concepts/cost-model).

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
| `READIE_DATA_DIR`                                        | overrides the data location (corpus, cache and metadata)                            |
| `RUNSC_BINARY`, `OCISPEC_BINARY`                         | the runtime and the OCI-spec generator (defaults `runsc`, `/usr/local/bin/ocispec`) |
| `READIE_CHECKPOINT_TIMEOUT`                              | how long a sandbox may take to checkpoint before `build` gives up (default `300`)   |
| `AZURE_ENDPOINT`, `AZURE_API_KEY`, `AZURE_MODEL_NAME`    | `corpus` only; the model endpoint, key and model name                                                     |
| `KAGGLE_API_TOKEN`                                       | `corpus` only; Kaggle access token. Required when a Kaggle source has weight        |
| `HF_TOKEN`                                               | `corpus` only; Hugging Face token. Required when a Hugging Face source has weight   |
| `READIE_CORPUS_TOTAL`                                    | `corpus` only; new items one run appends (default `2000`)                                 |
| `READIE_CORPUS_SOURCE_WEIGHTS`                           | `corpus` only; `source=weight` pairs over the defaults, for example `kaggle=3,agent_fix=0` |
| `READIE_CORPUS_BATCH`                                    | `corpus` only; candidates requested per call (default `10`)                          |
| `READIE_CORPUS_SEED`                                     | `corpus` only; makes the schedule repeatable                                        |
| `READIE_CORPUS_REWRITE`                                  | `corpus` only; `0` turns off the stand-in data rewrite (default on)                 |
| `READIE_CORPUS_REWRITE_MAX_CHARS`                        | `corpus` only; items with more code than this are not rewritten (default `60000`)   |
| `READIE_CORPUS_REWRITE_WORKERS`                          | `corpus` only; rewrite model calls at the same time (default `6`)                   |

## Known gaps

- `datasets`, `tokenizers` and `models` are carried through the schema and the
  plan, but only packages are pre-imported. Loading a model into the captured
  process is a much larger change, because it alters what a checkpoint costs to
  store.
- The rootfs is the full base image regardless of what the plan selects. This is
  deliberate: a new request distribution then regenerates only checkpoints, not
  tens of gigabytes of filesystem. A sandbox does carry far more than any one
  checkpoint needs.
