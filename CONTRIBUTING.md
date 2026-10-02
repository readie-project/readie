# Contributing

This document describes how to set up the repository, build and test each
component, change a contract, and submit a pull request. The documentation site
carries the same material in task form: see
[Set up your environment](https://readie.org/docs/contributing/setup),
[Repository map](https://readie.org/docs/contributing/repo-map),
[Conventions](https://readie.org/docs/contributing/conventions), and
[Pull requests](https://readie.org/docs/contributing/pull-requests).

## Components

The repository has six components. The five code components each have their own
build, their own tests, and their own entry in CI. The `nginx/` component is
configuration only. The root `Makefile` is an umbrella over all of them and owns
the one thing they share.

|                          | Language    | What it is                                                                |
| ------------------------ | ----------- | ------------------------------------------------------------------------- |
| [`nginx/`](nginx/)       | NGINX       | Client-facing proxy                                                       |
| [`router/`](router/)     | Python 3.13 | Placement, the cluster registry                                           |
| [`worker/`](worker/)     | Go          | Sandbox lifecycle, checkpoint/restore, executor I/O                       |
| [`executor/`](executor/) | Python 3.12 | Runs _inside_ every sandbox; unpickles and calls the function             |
| [`pkg/`](pkg/)           | Python 3.12 | `readie-client`, the `@remote` SDK (imported and distributed as `readie`) |
| [`pipeline/`](pipeline/) | Python 3.13 | Offline: corpus, package analysis, checkpoint planning and capture        |

`protos/` is the single source of truth for every wire contract.
`pipeline/Dockerfile` is the single source of truth for everything a checkpoint is
bound to. It has two targets: `pipeline`, the tool that captures checkpoints, and
`worker-base`, the base image that carries the pinned runsc, the root filesystem,
and the captured checkpoints. Both live in one file because a gVisor checkpoint
restores only into the filesystem it was captured from, through the runsc that
captured it.

`worker/Dockerfile` builds `FROM` that base and adds only the Go server and
`grpcurl`. A worker code change therefore rebuilds in seconds and does not touch
the 26 GB root filesystem layer.

The two Python version floors are deliberate. `pkg` and `executor` are installed
into someone else's process (a user's script and the sandbox image,
respectively), so they must work on whatever is already there. `router` and
`pipeline` control their own images and can pin their versions.

## Getting set up

Install [uv](https://docs.astral.sh/uv/), Go 1.25 or later, Docker, and
[buf](https://buf.build/docs/installation). The root `make lint` runs `buf lint`,
so buf is required for the full gate and not only when you change protos. The Go
linter comes from `make -C worker tools`.

```sh
make install          # sync every Python venv from its lockfile
make lint type test   # everything, the way CI runs it
make help             # every target
```

To run one component, use either `make -C router test` or the passthrough
`make router-test`.

Install the hooks so that the inexpensive checks run before you push and not in
CI:

```sh
uv tool install pre-commit && pre-commit install
```

## Working on one component

Each component has its own `Makefile` with the same five verbs, so you do not
need to remember which tool a given directory uses:

```sh
make install   # sync the venv / download modules
make fmt       # format and apply safe fixes
make lint      # check without modifying
make type      # mypy --strict, or nothing for Go
make test      # the suite
```

## Changing a proto

`protos/` feeds three languages. Generated code is committed so that a checkout
builds without protoc. This holds only if you regenerate the stubs after every
change:

```sh
make protos     # regenerate Go and Python stubs
buf lint && buf format -w
make docs-protos   # regenerate the docs site's protobuf reference (make docs-tools installs the generator)
```

The linter enforces two rules, and CI checks them:

- Changes are additive only. `buf breaking` runs against `main`. Use new field
  numbers, and never reuse or renumber existing ones, because a renumbered field
  silently reinterprets every message already in flight.
- A proto must not declare a `package`. Service names are bare
  (`/ProxyService/RequestExecution`). The compose healthcheck's `grpcurl` and the
  worker both depend on that, and an integration test asserts it. The `buf`
  `PACKAGE_DEFINED` rule is disabled in `buf.yaml` for this reason.

See [Change protos](https://readie.org/docs/contributing/changing-protos).

## Changing the executor protocol

`executor/`, `worker/internal/executor`, and `pkg` implement the same wire format
in two languages. A change must land in both languages together. A cross-language
golden fixture (`executor/tests/data/frames.golden.json`, read by the Go tests)
prevents silent drift.

If the framing changes, bump `executor_protocol` in the generation manifest. The
worker refuses a generation whose protocol it does not implement, and it names
the mismatch, instead of failing opaquely inside a restore. See
[Change the executor protocol](https://readie.org/docs/contributing/changing-executor-protocol).

## The shared `alpha`

`alpha` (seconds per MB) is the parameter that trades a checkpoint's size against
the import time it saves. The pipeline and the router must price size the same
way. The pipeline's greedy planner adds a package while it saves more than
`alpha·size`. The router selects the checkpoint that minimizes
`alpha·size + residual load time`. Both sides compute the same quantity, summed
over the dependency closure of what is not already resident (not only the items
that were literally requested).

The catalogue file keeps the two sides in step, so no pair of matching settings
is needed. The pipeline takes `READIE_ALPHA` as its planning weight, measures the
real value after capturing, and writes that value into `catalogue.json`. The
router has no `alpha` setting of its own and reads the value from the catalogue
it loads. A new generation and its catalogue must therefore be deployed together.
The value is tuned over time, so read it from the `Makefile` and the catalogue
and do not copy a number into a document.

## Conventions

Tests. Name the behavior and not the function:
`test_a_swallowed_result_raises_empty_result_carrying_the_logs`, not
`test_result_2`. A test that pins a past bug says so in a comment, because that
sentence is the only place the reasoning survives.

Comments. Explain why and not what. If a line looks wrong and is right, say why it
is right. If a line looks arbitrary and is load-bearing, say what breaks without
it.

Dependency injection. Constructors take their collaborators, and no module holds
mutable state. Both Python services have guard tests that walk the package and
fail if that stops being true.

Seams. Use `typing.Protocol` and not ABCs, so that a consumer declares the
interface it needs and an implementation satisfies it structurally. This matters
when an implementation lives in another process, as the worker client's transport
and codec seams do, and it lets a deployment swap an implementation without
subclassing anything.

## Commits and pull requests

Write the commit subject in the imperative mood, under about 72 characters, with
no trailing period. The body explains what was broken and why the fix has its
shape, because the diff already shows what changed.

CI must be green. It checks lint, types, and tests for all five components,
`go test -race` for the worker, `buf` for the protos, no generated-stub drift,
and that the images still build. The router and worker images are built in full,
and the worker is built against a stub base because it embeds no rootfs. The
pipeline's targets are tens of GB and cannot be built on a runner with 14 GB of
disk, so CI only lints them. State what a green run proves and nothing more.

## What is deliberately not done

The following items are listed so that nobody "fixes" one by accident.

- Router state is in memory. A restart loses sessions, and workers re-register.
  The alternative puts a datastore on the hot path of every placement decision.
- The rootfs is the full base image, regardless of what the planner selects.
  Rebuilding a worker must not mean rebuilding tens of gigabytes. For this reason
  the rootfs lives in `readie-worker-base`, and `worker/Dockerfile` inherits that
  image by tag and does not copy a rootfs in.
- Artifacts are baked in and not mounted. New checkpoints mean a new base image
  (`make generation`) and a new container. This gives one deployable with nothing
  to mis-mount, at the cost of an image of tens of GB. Redeploying worker code is
  cheap by contrast: `make worker-image`.
- `cloudpickle.loads` on router-supplied bytes in the client is an inherent
  remote-code-execution surface. It is confined behind a `ResultCodec` protocol
  and documented in [SECURITY.md](SECURITY.md).
- gVisor is amd64-only and works by intercepting syscalls, so the sandbox path
  cannot be exercised on Apple Silicon or in CI. State this limit and do not claim
  that a green run proves more than it does.
