"""Filterable diagnostics returned by the remote executor."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any


class IgnoredGlobalsWarning(UserWarning):
    """Client globals were discarded in favor of the session-owned namespace."""


def emit_remote_warnings(envelope: Any) -> None:
    """Emit recognized, well-formed diagnostics independently of log settings."""
    if not isinstance(envelope, dict):
        return
    records = envelope.get("warnings", ())
    if not isinstance(records, list):
        return
    for record in records:
        if not isinstance(record, dict) or record.get("code") != "ignored_globals":
            continue
        message, names = record.get("message"), record.get("names")
        if not isinstance(message, str) or not isinstance(names, list):
            continue
        if not all(isinstance(name, str) for name in names):
            continue
        # Both direct Client calls and decorated calls should point outside
        # readie. Unlike a fixed stacklevel, this also handles the async path.
        warnings.warn(
            message,
            IgnoredGlobalsWarning,
            skip_file_prefixes=(str(Path(__file__).parent),),
        )
