"""Parse a human-written size into a byte count.

The grammar mirrors the worker's ``parseBytes``
(``worker/internal/config/config.go``) so a value written one place means the
same thing everywhere: a bare integer is bytes, and binary (``Ki``/``Mi``/``Gi``/
``Ti``) or decimal (``KB``/``MB``/``GB``/``TB`` and their single-letter forms)
suffixes scale it. Used for every resource budget, memory and GPU memory alike.
"""

from __future__ import annotations

from readie.errors import ConfigurationError

# Longest suffixes first, so "Mi" is not read as "M". Binary suffixes are powers
# of two; the plain and *B forms are powers of ten.
_SUFFIXES: tuple[tuple[str, int], ...] = (
    ("KiB", 1 << 10),
    ("MiB", 1 << 20),
    ("GiB", 1 << 30),
    ("TiB", 1 << 40),
    ("Ki", 1 << 10),
    ("Mi", 1 << 20),
    ("Gi", 1 << 30),
    ("Ti", 1 << 40),
    ("KB", 1_000),
    ("MB", 1_000_000),
    ("GB", 1_000_000_000),
    ("TB", 1_000_000_000_000),
    ("K", 1_000),
    ("M", 1_000_000),
    ("G", 1_000_000_000),
    ("T", 1_000_000_000_000),
)


def parse_memory(value: str | int | None) -> int:
    """Return ``value`` as a byte count. ``None`` means "unset" and yields 0.

    A bare int is taken as bytes; a string may carry a size suffix. Raises
    ``ConfigurationError`` on a negative amount or an unparseable string, so a
    typo fails at the decorator rather than as a mysterious allocation later.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        # bool is an int subclass; a budget of True is a mistake, not 1 byte.
        msg = f"resource budget must be a size, not a bool: {value!r}"
        raise ConfigurationError(msg)
    if isinstance(value, int):
        if value < 0:
            msg = f"resource budget must not be negative, got {value}"
            raise ConfigurationError(msg)
        return value

    text = value.strip()
    if not text:
        return 0
    for suffix, scale in _SUFFIXES:
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            return _as_int(number, text) * scale
    return _as_int(text, text)


def _as_int(number: str, original: str) -> int:
    try:
        parsed = int(number)
    except ValueError as exc:
        msg = f"cannot parse memory size {original!r}"
        raise ConfigurationError(msg) from exc
    if parsed < 0:
        msg = f"resource budget must not be negative, got {original!r}"
        raise ConfigurationError(msg)
    return parsed
