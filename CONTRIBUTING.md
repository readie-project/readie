# Contributing

## The components

Five, each with its own build, its own tests, and its own entry in CI. The root
`Makefile` is an umbrella over them and owns the one thing they share.

|                          | Language     | What it is                                                                |
| ------------------------ | ------------ | ------------------------------------------------------------------------- |
| [`nginx/`](nginx/)       | NGINX        | Client-facing proxy                                                       |
| [`router/`](router/)     | Python 3.13  | Placement, the cluster registry                                           |
| [`worker/`](worker/)     | Go           | Sandbox lifecycle, checkpoint/restore, executor I/O                       |
| [`executor/`](executor/) | Python 3.12  | Runs _inside_ every sandbox; unpickles and calls the function             |
| [`pkg/`](pkg/)           | Python 3.12  | `readie-client`, the `@remote` SDK (imported and distributed as `readie`) |
| [`pipeline/`](pipeline/) | Python 3.13  | Offline: corpus, package analysis, checkpoint planning and capture        |

`protos/` is the single source of truth for every wire contract, and
`pipeline/Dockerfile` for everything a checkpoint is bound to: its two targets are
the tool that captures checkpoints (`pipeline`) and the base image carrying the
pinned runsc, the root filesystem and the captured checkpoints (`worker-base`) -
one file, because a gVisor checkpoint only restores into the filesystem it was
captured from, through the runsc that captured it.

`worker/Dockerfile` builds `FROM` that base and adds only the Go server and
grpcurl, so a worker code change rebuilds in seconds and never touches 26 GB.

The two Python version floors are deliberate. `pkg` and `executor` are installed
into someone else's process - a user's script and the sandbox image respectively

- so they must work on whatever is already there. `router` and `pipeline` control
  their own images and can pin.

## Getting set up

You need [uv](https://docs.astral.sh/uv/), Go 1.25+, Docker, and
[buf](https://buf.build/docs/installation) if you are touching the protos.

```sh
make install    # sync every Python venv from its lockfile
make lint type test   # everything, the way CI runs it
make help       # every target
```

Per component, either `make -C router test` or the passthrough `make router-test`.

Install the hooks so the cheap checks run before you push rather than in CI:

```sh
uv tool install pre-commit && pre-commit install
```

## Working on one component

Each has its own `Makefile` with the same five verbs, so you never have to
remember which tool a given directory uses:

```sh
make install   # sync the venv / download modules
make fmt       # format and apply safe fixes
make lint      # check without modifying
make type      # mypy --strict, or nothing for Go
make test      # the suite
```

## Changing a proto

`protos/` feeds three languages. Generated code is committed so a checkout builds
without protoc, which only stays true if you regenerate:

```sh
make protos     # regenerate Go and Python stubs
buf lint && buf format -w
```

Two rules the linter enforces and CI checks:

- **Additive only.** `buf breaking` runs against `main`. New field numbers, never
  reused or renumbered ones - a renumbered field silently reinterprets every
  message already in flight.
- **No `package` declaration.** Service names are bare
  (`/ProxyService/RequestExecution`). The compose healthcheck's grpcurl and the
  worker both depend on that, and an integration test asserts it. `buf`'s
  `PACKAGE_DEFINED` rule is disabled in `buf.yaml` for exactly this reason.

## Changing the executor protocol

`executor/`, `worker/internal/executor` and `pkg` implement the same wire format
in two languages. They cannot land apart, and a cross-language golden fixture
(`executor/tests/data/frames.golden.json`, read by the Go tests) exists so they cannot
drift silently.

If the framing changes, bump `executor_protocol` in the generation manifest. The
worker refuses a generation whose protocol it does not implement, by name, rather
than failing opaquely inside a restore.

## The shared `alpha`

`alpha` (seconds per MB) is the one knob that trades a checkpoint's disk size
against the import time it saves, and **the pipeline and the router must use the
same value**. The pipeline's greedy planner adds a package while it saves more
than `alpha·size`; the router selects the checkpoint minimising `alpha·size +
residual load time` - the same quantity. They are separate services, so the value
lives in each config (`pipeline` `Settings.alpha` / `READIE_ALPHA`, `router`
`Settings.alpha` / `ALPHA`), defaulting to `0.002` on both, with a docstring on
each pointing at the other. Changing one without the other makes the router
select against a cost the checkpoints were not built for.

## Conventions

**Tests.** Name the behaviour, not the function:
`test_a_swallowed_result_raises_empty_result_carrying_the_logs`, not
`test_result_2`. A test that pins a past bug should say so in a comment - that
sentence is the only place the reasoning survives.

**Comments** explain why, not what. If a line looks wrong and is right, say why
it is right. If it looks arbitrary and is load-bearing, say what breaks.

**Dependency injection** everywhere: constructors take their collaborators, and
no module holds mutable state. Both Python services have guard tests that walk
the package and fail if that stops being true.

**Seams are `typing.Protocol`**, not ABCs, so a consumer declares the interface
it needs and an implementation satisfies it structurally - which matters when an
implementation lives in another process, as the worker client's transport and
codec seams do, and lets a deployment swap one without subclassing anything.

## Commits and pull requests

Subject in the imperative, under ~72 characters, no trailing period. The body
should explain what was broken and why the fix is the right shape - the diff
already says what changed.

CI must be green: lint, types and tests for all five components, `go test -race`
for the worker, `buf` for the protos, no generated-stub drift, and the images
still building. The router and worker images are built in full - the worker
against a stub base, since it embeds no rootfs. The pipeline's targets are ~35 GB
and cannot be built on a runner with 14 GB of disk, so those are lint-checked
only; say what a green run proves rather than more.

## What is deliberately not done

Listed here so nobody "fixes" one by accident:

- **Router state is in memory.** A restart loses sessions and workers
  re-register. The alternative puts a datastore on the hot path of every
  placement decision.
- **The rootfs is the full base image**, regardless of what the planner selects.
  Rebuilding a worker should not mean rebuilding tens of gigabytes, which is why
  the rootfs lives in `readie-worker-base` and `worker/Dockerfile` inherits that
  image by tag rather than copying a rootfs in.
- **Artifacts are baked in, not mounted.** New checkpoints mean a new base image
  (`make generation`) and a new container. One deployable, nothing to mis-mount,
  at the cost of a ~35 GB image. Redeploying worker _code_ is cheap by contrast:
  `make worker-image`.
- **`cloudpickle.loads` on router-supplied bytes** in the client is an inherent
  remote-code-execution surface. It is confined behind a `ResultCodec` protocol
  and documented in [SECURITY.md](SECURITY.md), not papered over.
- **gVisor is amd64-only** and works by intercepting syscalls, so the sandbox
  path cannot be exercised on Apple Silicon or in CI. Say so rather than claiming
  a green run proves more than it does.
