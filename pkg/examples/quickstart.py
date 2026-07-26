"""Run a function on a worker.

CRFS_ROUTER_URI=localhost:50051 uv run python examples/quickstart.py
"""

from __future__ import annotations

import asyncio

import crfs
from crfs import remote


@remote
def add(a: int, b: int) -> int:
    print(f"adding {a} and {b}")  # arrives as a log line on the client
    return a + b


@remote
def load_frame(rows: int) -> int:
    import pandas as pd

    return len(pd.DataFrame({"x": range(rows)}))


def blocking() -> None:
    """The simplest form: call it like a function."""
    print("add(1, 2) =", add(1, 2))


async def concurrent() -> None:
    """Independent calls have independent sessions, so they run in parallel."""
    results = await asyncio.gather(*(add.aio(i, i) for i in range(4)))
    print("concurrent:", results)


def warm_session() -> None:
    """A session reuses one container, keeping whatever state the last call left.

    The trade is serialisation: a container is one interpreter behind one socket,
    so the router runs these one after another.
    """
    client = crfs.default_client()
    with client.session() as session:
        warm = load_frame.bind(session)
        print("first (cold):", warm(10))
        print("second (warm):", warm(20))


def handling_failure() -> None:
    """A remote exception arrives as EmptyResultError carrying the traceback."""

    @remote
    def explode() -> None:
        raise ValueError("boom")

    try:
        explode()
    except crfs.EmptyResultError as error:
        print("caught:", type(error).__name__)
        print("remote output:", error.logs)
    except crfs.ClusterUnavailableError:
        print("no router running; start the stack with `make up`")


if __name__ == "__main__":
    blocking()
    asyncio.run(concurrent())
    warm_session()
    handling_failure()
