"""``@remote`` and the callable it produces."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from typing import Any, Generic, ParamSpec, TypeVar, overload

from readie.budget import Budget, build_budgets
from readie.client import Client, Session

P = ParamSpec("P")
R = TypeVar("R")


class RemoteFunction(Generic[P, R]):
    """A function that runs on a worker instead of here.

    Calling it blocks; ``await fn.aio(...)`` does not; ``fn.local(...)`` skips
    the round trip entirely, which is what a unit test of the *body* wants.

    The wrapped function must be picklable by cloudpickle and its imports must
    exist in the worker image. Closing over a file handle, a database connection
    or a thread will fail at encode time with ``SerializationError``.
    """

    __slots__ = ("__dict__", "_budgets", "_client", "_func", "_gpu", "_session", "_timeout")

    # Written by functools.update_wrapper below; declared so the decorated
    # object type-checks as the drop-in replacement it is at runtime.
    __name__: str
    __qualname__: str
    __wrapped__: Callable[P, R]

    def __init__(
        self,
        func: Callable[P, R],
        *,
        client: Client | None = None,
        budgets: tuple[Budget, ...] = (),
        gpu: bool = False,
        timeout: float | None = None,
        session: Session | None = None,
    ) -> None:
        self._func = func
        self._client = client
        self._budgets = budgets
        self._gpu = gpu
        self._timeout = timeout
        self._session = session
        # Carries __name__, __doc__, __module__ and __wrapped__ across, so the
        # decorated object still introspects and documents like the original --
        # and so inspect.getsource can unwrap to it for estimation.
        functools.update_wrapper(self, func)

    @property
    def func(self) -> Callable[P, R]:
        """The undecorated function."""
        return self._func

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run remotely and block for the result."""
        start_time = time.perf_counter()
        result: R = self._resolve().call(
            self._func,
            args,
            kwargs,
            session=self._session,
            timeout=self._timeout,
            budgets=self._budgets,
            gpu=self._gpu,
        )
        end_time = time.perf_counter()
        print(f"Execution completed in {end_time - start_time}s")
        return result

    async def aio(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run remotely and await the result."""
        start_time = time.perf_counter()
        result: R = await self._resolve().acall(
            self._func,
            args,
            kwargs,
            session=self._session,
            timeout=self._timeout,
            budgets=self._budgets,
            gpu=self._gpu,
        )
        end_time = time.perf_counter()
        print(f"Execution completed in {end_time - start_time}s")
        return result

    def local(self, *args: P.args, **kwargs: P.kwargs) -> R:
        """Run here, in this process. No router, no worker, no serialisation."""
        return self._func(*args, **kwargs)

    def bind(
        self,
        session: Session | None = None,
        *,
        client: Client | None = None,
        timeout: float | None = None,
    ) -> RemoteFunction[P, R]:
        """Return a copy bound to a session, client or deadline.

        A copy rather than a mutation: the decorated object is module-level and
        shared, so binding a session onto it in place would leak that session
        into every other caller in the process.
        """
        return RemoteFunction(
            self._func,
            client=client if client is not None else self._client,
            budgets=self._budgets,
            gpu=self._gpu,
            timeout=timeout if timeout is not None else self._timeout,
            session=session if session is not None else self._session,
        )

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        """Support decorating methods.

        Without this the descriptor protocol never runs and ``self`` is dropped,
        so a decorated method silently receives the wrong first argument.
        """
        if instance is None:
            return self
        # ParamSpec cannot express "the same signature minus its first
        # parameter", so the bound form is typed loosely and the descriptor
        # contract is pinned by a test instead.
        call: Callable[..., R] = self.__call__
        return functools.partial(call, instance)

    def __repr__(self) -> str:
        """Show the wrapped function and any bound session."""
        name = getattr(self._func, "__qualname__", repr(self._func))
        bound = f", session={self._session.id!r}" if self._session is not None else ""
        return f"<remote {name}{bound}>"

    def _resolve(self) -> Client:
        if self._client is not None:
            return self._client
        return default_client()


@overload
def remote(func: Callable[P, R], /) -> RemoteFunction[P, R]: ...


@overload
def remote(
    *,
    client: Client | None = ...,
    timeout: float | None = ...,
    session: Session | None = ...,
    gpu: bool = ...,
    memory: str | int | None = ...,
    max_memory: str | int | None = ...,
    gpu_memory: str | int | None = ...,
    max_gpu_memory: str | int | None = ...,
) -> Callable[[Callable[P, R]], RemoteFunction[P, R]]: ...


def remote(
    func: Callable[P, R] | None = None,
    /,
    *,
    client: Client | None = None,
    timeout: float | None = None,
    session: Session | None = None,
    gpu: bool = False,
    memory: str | int | None = None,
    max_memory: str | int | None = None,
    gpu_memory: str | int | None = None,
    max_gpu_memory: str | int | None = None,
) -> RemoteFunction[P, R] | Callable[[Callable[P, R]], RemoteFunction[P, R]]:
    """Mark a function for remote execution.

    Usable bare or with arguments::

        @remote
        def f(x): ...

        @remote(gpu=True, memory="2Gi", max_memory="8Gi")
        def g(x): ...

    ``gpu=True`` routes the call to a GPU worker (a ``gpu_memory`` budget implies
    it too). ``memory``/``gpu_memory`` set the container's initial budget and
    ``max_memory``/``max_gpu_memory`` the ceiling the worker may auto-expand to;
    each accepts a byte count or a size string (``"512Mi"``, ``"4Gi"``). An unset
    budget falls back to the cluster default. Decoration itself does no work and
    opens no connection, so a module of ``@remote`` definitions imports as fast
    as one without them.
    """
    budgets = build_budgets(
        memory=memory,
        max_memory=max_memory,
        gpu_memory=gpu_memory,
        max_gpu_memory=max_gpu_memory,
    )

    def decorate(target: Callable[P, R]) -> RemoteFunction[P, R]:
        return RemoteFunction(
            target, client=client, budgets=budgets, gpu=gpu, timeout=timeout, session=session
        )

    if func is not None:
        return decorate(func)
    return decorate


# ---------------------------------------------------------------------------
# The process-wide default client.
#
# Module state, knowingly: `@remote` at module scope has to reach *some* client,
# and requiring one at decoration time would mean constructing it at import
# time. It is lazy, replaceable through `configure`, and never consulted when a
# client is passed explicitly -- so nothing in this package depends on it.
# ---------------------------------------------------------------------------
_default: Client | None = None


def default_client() -> Client:
    """Return the process-wide client, creating it from the environment."""
    global _default  # noqa: PLW0603 - the documented single piece of module state
    if _default is None:
        _default = Client()
    return _default


def configure(client: Client | None = None, **settings: Any) -> Client:
    """Install the process-wide client.

    Pass a client, or keyword settings to build one.

    Replacing an existing default closes it first, so its channel is not
    orphaned.
    """
    global _default  # noqa: PLW0603

    from readie.config import Settings  # noqa: PLC0415 - avoids a cycle at import time

    if client is not None and settings:
        msg = "pass either a client or settings keywords, not both"
        raise TypeError(msg)

    previous = _default
    _default = client if client is not None else Client(Settings(**settings))
    if previous is not None and previous is not _default:
        previous.close()
    return _default


def reset() -> None:
    """Drop the process-wide client, closing it. Mainly for tests."""
    global _default
    previous, _default = _default, None
    if previous is not None:
        previous.close()
