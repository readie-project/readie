---
title: Conventions
sidebar_position: 5
description: Coding conventions of the repository, covering dependency injection, tests, tooling, Python version floors, and the shared alpha constant.
---

The code base relies on the conventions below. Tools enforce most of them, so a pull request that breaks one fails `make lint type test`. Conventions enforced only by review are marked *(Review only.)*

## Constructor dependency injection

A class receives the objects it needs as constructor arguments. It does not create them and does not read global state. The router `Scheduler` takes its helpers as arguments:

```python
class Scheduler:
    def __init__(
        self,
        state: ClusterState,
        selector: WorkerSelector,
        clock: Clock,
        catalogues: Mapping[str, Catalogue] | None = None,
    ) -> None:
        self._state = state
        self._selector = selector
        self._clock = clock
```

A test passes a fake clock and a fake selector, and production passes the real ones. No monkeypatching is required.

Two related rules apply:

- Modules hold no mutable module-level state, and no singleton is created at import time. The router test `router/tests/unit/test_structure.py` fails if a module holds a `Scheduler` or a similar object.
- Comments explain why. If a line looks wrong but is correct, the comment states the reason. If a line looks arbitrary but matters, the comment states what breaks without it. *(Review only.)*

## Seams as small interfaces

A seam is a place where one implementation can be swapped for another. Seams are small interfaces owned by the code that uses them.

- In Python, use `typing.Protocol` and not an abstract base class.
- In Go, use an interface.

A `Protocol` is satisfied by shape, so an implementation does not inherit from anything. This matters when an implementation lives in another process. The worker-selection code of the router shows the pattern:

```python
class WorkerScorer(Protocol):
    """Ranks eligible workers. Lower is better."""

    def score(self, worker: WorkerView, demand: Demand) -> tuple[float, ...]:
        ...
```

Any class with a matching `score` method can serve as a scorer. The client SDK follows the same pattern with `ResultCodec`, which lets a deployment replace how results are decoded.

## Test names

A test name describes the behavior and not the function under test. For example, `test_the_version_is_two` and `test_the_golden_encoding_decodes_to_its_payload` describe behavior, whereas `test_result_2` does not.

If a test pins a bug that was fixed in the past, a comment on the test says so. That comment is often the only record of why the test exists. *(Review only.)*

Every behavior change comes with a test. The pull request template asks for a test that fails without the change. See [Pull requests](/docs/contributing/pull-requests).

## Tooling per language

| Language | Format | Lint | Types | Tests |
| --- | --- | --- | --- | --- |
| Python (all four components) | `ruff format` | `ruff check` | `mypy` with `strict = true` | `pytest` |
| Go (`worker/`) | `go fmt` | `golangci-lint`, plus `go vet` | the compiler | `go test -race` |
| Protobuf | `buf format` | `buf lint` | not applicable | not applicable |

Additional details:

- Python lines are at most 100 characters.
- `make fmt` in a component formats the code and applies safe lint fixes. `make lint` only checks.
- Ruff bans `print()` and relative imports in the router, and enforces Google-style docstrings. Each component's `pyproject.toml` lists its exact rules.
- The Go worker runs its tests under the race detector, which finds data races in concurrent code. The worker contains a large amount of concurrent code.
- `.editorconfig` sets UTF-8, LF line endings, four-space indents, tabs for Go and Makefiles, and two spaces for YAML, JSON, TOML, and protos.
- Generated code is never edited by hand and is skipped by the formatters.

## Python version floors

Two components are held to Python 3.12 exactly, and two use Python 3.13.

| Component | Python | Reason |
| --- | --- | --- |
| `pkg/` | 3.12 | Runs inside the user's own program. |
| `executor/` | 3.12 | Runs inside the sandbox image, whose Python is 3.12. |
| `router/` | 3.13 | Has its own container image. |
| `pipeline/` | 3.13 | Has its own container image. |

The client pickles the function, and the executor unpickles it. Pickled functions are safe to share only between the same Python minor version. For this reason `readie.Client()` refuses to start unless the interpreter is exactly 3.12. The constant is `REQUIRED_PYTHON` in `pkg/src/readie/_compat.py`.

If the Python version of the executor changes, update these three values together: `REQUIRED_PYTHON`, the `requires-python` line in `pkg/pyproject.toml`, and the `requires-python` line in `executor/pyproject.toml`. CI tests `pkg` and `executor` on 3.12, and `router` and `pipeline` on 3.13.

## Shared `alpha` constant

[`alpha`](/docs/concepts/cost-model) is a number in seconds per megabyte. It sets how much the size of a checkpoint counts against the import time that the checkpoint saves. Two programs use the constant, and the values must agree:

- The planner in the pipeline keeps adding a package to a checkpoint while the time the package saves is greater than `alpha` times its size.
- The router selects the checkpoint with the lowest `alpha * size + remaining load time`.

If the values differ, the router chooses against a cost for which the checkpoints were not built.

The two values are stored in different places:

- The pipeline reads `READIE_ALPHA`. The root `Makefile` sets it for `make capture`.
- The pipeline writes its value into each catalogue file, such as `catalogues/cpu.json`. The router reads `alpha` from the catalogue file it loads.

Rebuild the catalogue and the checkpoints together, and do not edit `alpha` in a catalogue by hand. See [Cost model](/docs/concepts/cost-model).

## Generated code and protos

- Generated stubs are never edited by hand. Regenerate them with `make protos`. See [Change protos](/docs/contributing/changing-protos).
- A proto never gains a `package` line.

## What's next

- [Pull requests](/docs/contributing/pull-requests)
- [Repository map](/docs/contributing/repo-map)
