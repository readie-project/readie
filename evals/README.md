# readie-evals

An LLM-driven evaluation harness that measures Readie on real-world, multi-domain
Python workloads. The agent uses Claude on Azure AI Foundry (the `anthropic` SDK's
`AnthropicFoundry` client).

The agent **freshly fetches** code from three sources — **Kaggle**
kernels, **HuggingFace** model cards, and its own **generation** — adapts each into
a self-contained, CPU-only, picklable function, tags it with its `{source,
category}`, runs it through the `readie` client, and records the workload's
**imports** and **execution time**. Fetching and running are both resumable: pause
at any point and re-invoke, and each picks up where it left off.

This component builds a *fresh* corpus for evaluation. It deliberately does **not**
reuse `pipeline/data/datasets/cpu.json`, which is the planner's training data.

## How it works

Three phases, each a `readie-evals` subcommand, plus a fourth entry point,
`sample`, for an unplanned, mixed batch instead of a grid fill:

1. **`fetch`** — for each `(source, category)` short of its target, source raw
   candidates and turn them into runnable tasks. Generation writes code directly;
   Kaggle/HuggingFace code is rewritten by the agent into a self-contained function.
   Every task uses a **real dataset** — downloaded from inside `task()` itself
   (`sklearn.datasets.fetch_*`, `pandas.read_csv(<public URL>)`, HuggingFace
   `datasets.load_dataset(...)`, etc. — the sandbox has real network egress, but
   only what the function downloads itself, and never Kaggle's own
   authenticated competition-data download), not synthetic placeholder data.
   Every task is validated (it must compile, define `task()`, and avoid GPU/DL
   frameworks) and appended to `out/corpus.jsonl` (append-only: nothing already
   there is ever rewritten or discarded). A task may also be a **session**:
   several cells that run in order against one warm container, either
   simulating a Jupyter notebook (cells build on state an earlier one left behind,
   e.g. a dataset downloaded once and cached to a scratch path) or a developer
   retrying inside one live session (each cell is an independent, revised
   attempt). A notebook session can come from the model generating one from
   scratch (`generated-notebook`) or from adapting a real Kaggle kernel's or
   HuggingFace card's own cells (`kaggle-notebook`, `huggingface-notebook`); retry
   sessions only make sense for generated code. All four are opt-in via
   `--sources` (see below).
2. **`run`** — execute each task through Readie and time the comparison that is the
   point of the system: a **checkpoint** call (the normal path — the router restores a
   checkpoint that already imported the needed packages) against a **cold-start** call
   (`disable_optimized_execution`, which tells the router to skip restore and start
   from scratch). Both are fresh containers, so the gap is exactly what checkpoint
   restore saves. A session task (more than one cell) runs the whole sequence
   through one session per side instead, so the two numbers are the *total* time
   for all its cells, checkpoint-restored vs. cold-started once each. A task that
   raises is handed to the agent for **one** repair attempt (standalone tasks
   only — a session failure could belong to any cell); if it still fails it is
   recorded as `failed`. Exits immediately (before touching any task) if the
   router isn't reachable, rather than failing task-by-task. Results are appended
   to `out/results.jsonl` (JSON Lines — one record per line, no database,
   append-only like the corpus).
3. **`report`** — roll results up by domain and source (success rate, checkpoint vs
   cold-start timing percentiles, and the **speedup** = cold-start ÷ checkpoint) and
   list the most common imports, into `out/reports/`.

**`sample`** takes a different path into the same corpus: instead of filling a
`(source, category)` grid toward a target, it always *adds* `--count` fresh tasks
(10 by default, appended alongside whatever is already there), each from a
distinct, randomly chosen `(source, category)` pair across every source — Kaggle,
HuggingFace, plain generation, and all four session variants — and every domain.
It never consults what the corpus already has, so a re-run adds a fresh,
independent batch rather than topping one up; think of it as simulating an
unplanned user session rather than deliberately building out coverage. `run`/
`report` work on its output exactly as they do on `fetch`'s.

### Domains and sources

The 15 domains mirror the pipeline's corpus taxonomy (EDA, Feature Engineering,
Data Preprocessing, Data Science, ML, NLP, Model Inference, Computer Vision, Image
Processing, Time Series, Recommender Systems, Anomaly Detection, ETL, Data
Visualization, Graph Processing). On CPU, "deep learning" means inference, which
lands under NLP/CV/Model Inference. Sources are `kaggle`, `huggingface`,
`generated` by default; the four session sources (`generated-notebook`,
`generated-retry`, `kaggle-notebook`, `huggingface-notebook`) are opt-in via
`--sources` since each session costs several LLM/sandbox calls instead of one.
Kaggle and HuggingFace tasks carry the source kernel/model's own identifier in
`slug` ("user/kernel-slug", "org/model-name") and its URL in `provenance` (both
also in `out/results.jsonl`, for tracing a result back to its source without
cross-referencing the corpus).

## Setup

```sh
make install                 # sync the venv (includes the [fetch] extra)
cp .env.example .env         # then fill in the AZURE_* creds and KAGGLE_API_TOKEN
```

The `readie` client is a path dependency on `../pkg`, so this component targets
Python 3.12 to match it.

## Running an evaluation

Real execution needs the CPU stack up:

```sh
make -C .. generation FLAVOR=cpu   # once: capture checkpoints (amd64 + gVisor, ~35 GB)
make -C .. run-local               # bring up the stack on localhost:50051
```

Then, from `evals/`:

```sh
uv run readie-evals fetch --per-cell 3          # build the corpus (all sources/domains)
uv run readie-evals run                         # measure checkpoint vs cold-start
uv run readie-evals report                      # summarize the latest run
# or the whole chain:
uv run readie-evals all
```

Useful flags: `fetch --sources generated kaggle --categories "Machine Learning"`,
`fetch --sources generated-notebook generated-retry kaggle-notebook
huggingface-notebook` (multi-cell session tasks), `sample --count 10` (a fresh,
mixed batch across every source and domain, no repeated pairs; add `--seed N` for
a repeatable one), `run --target local` (offline dry run of generated tasks via
the client's in-process path — no gVisor, no keys; real sessions and plain
Kaggle/HuggingFace tasks are skipped as untrusted for this path), `run --run-id
<id>` (group a campaign), `run --limit N`.

### Pause and resume

Both phases are idempotent. `fetch` only fills the shortfall per `(source,
category)`, so interrupting and re-running continues fetching. `run` skips any task
already recorded for the run id, so Ctrl-C and re-run continues measuring. To
re-measure the same corpus from scratch, pick a new `--run-id`.

## Configuration

See `.env.example`. Key variables: `AZURE_API_KEY`, `AZURE_ENDPOINT` (the Foundry
Anthropic endpoint), `AZURE_MODEL_NAME` (the same variables the pipeline uses),
`KAGGLE_API_TOKEN` (the `KGAT_...` token from Kaggle settings), `READIE_ROUTER_URI`,
`READIE_TLS`, `READIE_EVALS_PER_CELL`, `READIE_EVALS_OUT_DIR`.

## Developing

```sh
make lint type test    # ruff, mypy --strict, pytest
```

Tests use fakes for the LLM and the fetchers and exercise execution through the
client's in-process `local` path, so the suite runs offline with no API key and no
gVisor.
