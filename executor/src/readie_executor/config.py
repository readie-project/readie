"""Executor settings.

Read once at startup into a frozen value. A plain dataclass rather than
pydantic: this package is installed into the sandbox rootfs, where every
dependency is a package that must exist in the image and be resident in memory
at checkpoint time.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_SOCKET_NAME = "executor.sock"
DEFAULT_MODE = "sandbox"

def get_current_mode() -> Literal["capture", "measure", "sandbox"]:
    return os.getenv("EXECUTOR_MODE", "") or DEFAULT_MODE


class ConfigError(Exception):
    """The executor was started with an unusable environment."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the executor needs to know at startup."""

    socket_dir: str
    """Directory the socket is bound in. Bind-mounted from the worker."""

    socket_name: str = DEFAULT_SOCKET_NAME
    chunk_size: int = DEFAULT_CHUNK_SIZE

    preimport: tuple[str, ...] = field(default_factory=tuple)
    """Modules to import before the checkpoint is taken.

    This is the entire point of a checkpoint: the captured process already has
    these resident, so a restore skips the import cost that dominates a cold
    start.
    """

    @property
    def socket_path(self) -> str:
        """Full path of the unix socket the worker dials."""
        return str(Path(self.socket_dir) / self.socket_name)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build settings from the sandbox environment.

        ``EXECUTOR_DIR`` is required and named by the OCI spec the worker and
        the pipeline both generate. Failing here with an explanation beats
        failing later with a ``KeyError`` that names a variable and not its
        purpose.
        """
        source = os.environ if env is None else env

        socket_dir = (source.get("EXECUTOR_DIR") or "").strip()
        if not socket_dir:
            msg = (
                "EXECUTOR_DIR is unset; it names the directory the executor "
                "binds its socket in and is set by the sandbox spec"
            )
            raise ConfigError(msg)

        return cls(
            socket_dir=socket_dir,
            socket_name=source.get("EXECUTOR_SOCKET_NAME") or DEFAULT_SOCKET_NAME,
            chunk_size=_int(source, "EXECUTOR_CHUNK_SIZE", DEFAULT_CHUNK_SIZE),
            preimport=parse_preimport(source.get("READIE_PREIMPORT", "")),
        )


def parse_preimport(raw: str) -> tuple[str, ...]:
    """Split a comma-separated module list, dropping blanks and duplicates.

    Order is preserved rather than sorted: a caller that lists ``numpy`` before
    ``pandas`` may be expressing a dependency, and reordering imports can change
    which module initialises first.
    """
    seen: dict[str, None] = {}
    for part in raw.split(","):
        name = part.strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{name} must be an integer, got {raw!r}"
        raise ConfigError(msg) from exc
    if value <= 0:
        msg = f"{name} must be positive, got {value}"
        raise ConfigError(msg)
    return value


def _float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        msg = f"{name} must be a number, got {raw!r}"
        raise ConfigError(msg) from exc
    if value < 0:
        msg = f"{name} must not be negative, got {value}"
        raise ConfigError(msg)
    return value
