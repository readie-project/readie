"""Function reconstruction preserves captures without importing client state."""

from __future__ import annotations

import builtins
from functools import partial
from textwrap import dedent
from types import FunctionType
from typing import Any, cast

import pytest

from readie_executor.globals import bind_session_globals


def function(source: str) -> FunctionType:
    namespace: dict[str, Any] = {"__name__": "__readie_globals_unit_test__"}
    exec(dedent(source), namespace)  # noqa: S102 - trusted test source
    func = namespace["task"]
    assert isinstance(func, FunctionType)
    return func


def namespace() -> dict[str, Any]:
    return {"__builtins__": vars(builtins).copy(), "__name__": "__readie_session__"}


def test_functions_share_the_exact_dictionary_without_modifying_the_original():
    original = function("""
        client_value = 99
        def task(value):
            global client_value
            client_value = value
            return len([client_value])
    """)
    state = namespace()
    first, warnings = bind_session_globals(original, state)
    second, _ = bind_session_globals(original, state)
    assert first.__globals__ is second.__globals__ is state
    assert "client_value" not in state
    assert first(42) == 1
    assert state["client_value"] == 42
    assert original.__globals__["client_value"] == 99
    assert warnings


def test_defaults_closures_annotations_and_metadata_are_preserved():
    factor = 7

    def task(value: int = 6, *, offset: int = 0) -> int:
        """Example function."""
        return value * factor + offset

    task.__dict__["custom"] = {"tag": "preserved"}
    bound, _ = bind_session_globals(task, namespace())
    assert bound() == 42
    assert bound(offset=1) == 43
    assert bound.__closure__ is task.__closure__
    assert bound.__defaults__ is task.__defaults__
    assert bound.__kwdefaults__ is task.__kwdefaults__
    assert bound.__annotations__ == task.__annotations__
    assert bound.__name__ == task.__name__
    assert bound.__qualname__ == task.__qualname__
    assert bound.__module__ == task.__module__
    assert bound.__doc__ == task.__doc__
    assert bound.__dict__ == task.__dict__


def test_generic_function_type_parameters_are_preserved():
    original = function("""
        def task[T](value: T) -> T:
            return value
    """)
    bound, _ = bind_session_globals(original, namespace())
    assert bound.__type_params__ is original.__type_params__
    assert bound.__annotations__ == original.__annotations__
    assert bound(42) == 42


def test_defaults_and_closure_mutations_are_not_merged_into_session_globals():
    captured: list[int] = []

    def task(default: list[int] = captured) -> tuple[list[int], list[int]]:
        captured.append(1)
        default.append(2)
        return captured, default

    state = namespace()
    bound, _ = bind_session_globals(task, state)
    assert bound() == ([1, 2], [1, 2])
    assert "captured" not in state
    assert "default" not in state


def test_unreferenced_bindings_and_non_string_keys_are_discarded_too():
    original = function("""
        def task():
            return globals()
    """)
    original.__globals__.pop("task")
    cast("dict[Any, Any]", original.__globals__)[42] = object()
    bound, warnings = bind_session_globals(original, namespace())
    assert warnings
    assert 42 not in cast("dict[Any, Any]", bound.__globals__)
    original.__globals__.update({"z": 1, "a": 2})
    bound, warnings = bind_session_globals(original, namespace())
    assert warnings
    assert "a" not in bound.__globals__
    assert "z" not in bound.__globals__


class CallableObject:
    def __call__(self) -> int:
        return 42


@pytest.mark.parametrize(
    "func", [len, partial(len, []), CallableObject(), CallableObject().__call__]
)
def test_non_function_callables_are_rejected(func):
    state = namespace()
    with pytest.raises(TypeError, match="plain Python functions only"):
        bind_session_globals(func, state)
    assert set(state) == {"__builtins__", "__name__"}


def test_falsy_globals_remain_present_and_readable():
    state = namespace()
    reader, _ = bind_session_globals(
        function("""
        def task():
            return "saved" in globals(), saved
    """),
        state,
    )
    values: tuple[object, ...] = (None, False, 0, "", [])
    for value in values:
        state["saved"] = value
        present, returned = reader()
        assert present is True
        assert returned is value


def test_global_imports_are_shared_and_deletion_removes_the_binding():
    state = namespace()
    importer, _ = bind_session_globals(
        function("""
        def task():
            global session_math
            import math as session_math
    """),
        state,
    )
    reader, _ = bind_session_globals(
        function("""
        def task():
            return session_math.isqrt(81)
    """),
        state,
    )
    deleter, _ = bind_session_globals(
        function("""
        def task():
            global session_math
            del session_math
    """),
        state,
    )
    importer()
    assert reader() == 9
    deleter()
    assert "session_math" not in state


def test_locals_and_the_submitted_function_name_do_not_enter_session_globals():
    state = namespace()
    bound, _ = bind_session_globals(
        function("""
        def task():
            local_value = 42
            return local_value
    """),
        state,
    )
    assert "task" not in state
    assert bound() == 42
    assert "local_value" not in state
    assert "task" not in state


def test_a_published_helper_reads_subsequent_updates_to_the_shared_namespace():
    state = namespace()
    publisher, _ = bind_session_globals(
        function("""
        def task():
            global helper, saved
            saved = 42
            def helper():
                return saved
    """),
        state,
    )
    update, _ = bind_session_globals(
        function("""
        def task():
            global saved
            saved += 1
            return helper()
    """),
        state,
    )
    publisher()
    assert update() == 43
