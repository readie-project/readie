"""The local interpreter must match the executor's."""

from __future__ import annotations

import pytest

from readie._compat import REQUIRED_PYTHON, check_python_version
from readie.client import Client
from readie.errors import IncompatiblePythonError, ReadieError


def test_required_minor_version_is_accepted() -> None:
    check_python_version((*REQUIRED_PYTHON, 7))


@pytest.mark.parametrize("version", [(3, 11), (3, 13), (2, 12)])
def test_other_versions_raise(version: tuple[int, int]) -> None:
    with pytest.raises(IncompatiblePythonError, match=r"requires Python 3\.12"):
        check_python_version(version)


def test_error_is_a_readie_error() -> None:
    assert issubclass(IncompatiblePythonError, ReadieError)


def test_client_construction_checks_the_running_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("readie._compat.sys.version_info", (3, 11, 0))
    with pytest.raises(IncompatiblePythonError):
        Client()
