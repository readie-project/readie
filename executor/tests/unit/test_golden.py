"""The cross-language framing fixture, decoded by the Python implementation.

worker/internal/executor decodes the same file with its own. Two hand-written
implementations of one wire format drift; a shared fixture makes that a test
failure rather than a production incident months later.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crfs_executor.protocol import PROTOCOL_VERSION, read_message, write_message
from tests.fakes import RecordingWriter, ScriptedReader

FIXTURE = Path(__file__).parents[1] / "data" / "frames.golden.json"


def load() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())  # type: ignore[no-any-return]


def cases() -> list[dict[str, str]]:
    return load()["cases"]  # type: ignore[return-value]


def ids() -> list[str]:
    return [c["name"] for c in cases()]


def test_the_fixture_matches_this_protocol_version():
    # A version bump without regenerating the fixture leaves both suites
    # validating the old format.
    assert load()["protocol_version"] == PROTOCOL_VERSION


@pytest.mark.parametrize("case", cases(), ids=ids())
def test_the_golden_encoding_decodes_to_its_payload(case):
    encoded = bytes.fromhex(case["encoded_hex"])
    expected = bytes.fromhex(case["payload_hex"])

    assert read_message(ScriptedReader([encoded]), chunk_size=4096) == expected


@pytest.mark.parametrize("case", cases(), ids=ids())
def test_the_golden_encoding_decodes_when_dribbled_one_byte_at_a_time(case):
    encoded = bytes.fromhex(case["encoded_hex"])
    reader = ScriptedReader([encoded[i : i + 1] for i in range(len(encoded))])

    assert read_message(reader, chunk_size=4096) == bytes.fromhex(case["payload_hex"])


@pytest.mark.parametrize("case", cases(), ids=ids())
def test_writing_the_payload_reproduces_a_decodable_message(case):
    # Not byte-equality with the fixture: chunk boundaries are a sender's
    # choice, so the contract is that any framing of the same payload decodes
    # back to it. Byte-equality would pin an implementation detail.
    payload = bytes.fromhex(case["payload_hex"])
    writer = RecordingWriter()
    write_message(writer, payload, chunk_size=64)

    assert read_message(ScriptedReader([writer.data]), chunk_size=64) == payload
