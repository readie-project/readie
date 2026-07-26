"""Identifier generation.

The previous client hardcoded ``request_id = "request_id"`` and
``session_id = "session-id"`` for every call in every process. Request ids are
the router's lease key, and session ids are its affinity key, so the whole fleet
shared one lease and one container queue. These are the fix, and they are the
reason session affinity can exist at all.
"""

from __future__ import annotations

from uuid import uuid4


def new_request_id() -> str:
    """Return a fresh, globally unique request identifier."""
    return f"req-{uuid4().hex}"


def new_session_id() -> str:
    """Return a fresh, globally unique session identifier."""
    return f"sess-{uuid4().hex}"
