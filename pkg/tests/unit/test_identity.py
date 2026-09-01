"""Identifiers must actually be unique."""

from __future__ import annotations

from readie.identity import new_request_id, new_session_id


def test_request_ids_are_unique() -> None:
    # The old client sent the literal string "request_id" for every call in
    # every process, so the router's leases collided fleet-wide.
    assert len({new_request_id() for _ in range(1000)}) == 1000


def test_session_ids_are_unique() -> None:
    # Likewise "session-id", which under affinity would pin the entire fleet to
    # one container and one serialised queue.
    assert len({new_session_id() for _ in range(1000)}) == 1000


def test_ids_are_prefixed_so_logs_are_readable() -> None:
    assert new_request_id().startswith("req-")
    assert new_session_id().startswith("sess-")
