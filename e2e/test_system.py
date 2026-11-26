"""Exercise the installed Readie SDK against a configured TLS endpoint."""

import asyncio
import os

import pytest
import readie
from readie.codec import CloudpickleCodec
from readie.config import CHUNK_SIZE
from readie.protocol import CallRef, RequestEncoder


def client_settings(*, chunk_size: int = CHUNK_SIZE) -> readie.Settings:
    if not os.environ.get("READIE_AUTH_TOKEN"):
        pytest.fail("READIE_AUTH_TOKEN must be configured for the system suite")
    return readie.Settings(
        router_uri=os.environ["READIE_E2E_ENDPOINT"],
        chunk_size=chunk_size,
        timeout=45,
        tls=True,
        stream_logs=False,
    )


def test_round_trip():
    def add(left, right):
        return left + right

    with readie.Client(client_settings()) as client:
        assert readie.remote(client=client)(add)(40, 2) == 42


def test_chunked_async_payload():
    def echo(payload):
        return payload

    chunk_size = 1024
    payload = bytes(range(256)) + b"".join(
        block.to_bytes(2, "big") for block in range(2048)
    )
    codec = CloudpickleCodec()
    # Pickle overhead depends on the installed SDK and cloudpickle. Check the
    # exact shape after serialization and add one byte if it ends on a boundary.
    encoded = codec.encode_call(echo, (payload,), {})
    if len(encoded) % chunk_size == 0:
        payload += b"!"
        encoded = codec.encode_call(echo, (payload,), {})
    messages = list(
        RequestEncoder(chunk_size=chunk_size).encode(CallRef("preflight", ""), encoded)
    )
    lengths = [len(message.payload) for message in messages[1:]]
    assert len(lengths) >= 3
    assert all(length == chunk_size for length in lengths[:-1])
    assert 0 < lengths[-1] < chunk_size

    async def run():
        async with readie.Client(client_settings(chunk_size=chunk_size)) as client:
            assert await readie.remote(client=client)(echo).aio(payload) == payload

    asyncio.run(run())


def test_session_retains_state_and_isolates_other_sessions():
    def write_marker():
        import builtins

        builtins._readie_e2e_marker = "session value"
        return True

    def read_marker():
        import builtins

        return getattr(builtins, "_readie_e2e_marker", None)

    with readie.Client(client_settings()) as client:
        with client.session() as original:
            assert readie.remote(client=client, session=original)(write_marker)()
            assert (
                readie.remote(client=client, session=original)(read_marker)()
                == "session value"
            )
        with client.session() as separate:
            assert readie.remote(client=client, session=separate)(read_marker)() is None


def test_remote_exception_preserves_details_and_output():
    def fail_remotely():
        print("readie e2e stdout marker")
        raise ValueError("expected E2E failure")

    with (
        readie.Client(client_settings()) as client,
        pytest.raises(readie.RemoteExecutionError) as caught,
    ):
        readie.remote(client=client)(fail_remotely)()

    error = caught.value
    assert error.remote_type == "ValueError"
    assert error.remote_message == "expected E2E failure"
    assert "in fail_remotely" in error.remote_traceback
    assert "readie e2e stdout marker" in "".join(error.logs)
    assert error.worker_id
    assert error.container_id


def test_tls_endpoint_requires_token(monkeypatch):
    def add(left, right):
        return left + right

    authorized = client_settings()
    with monkeypatch.context() as environment:
        environment.delenv("READIE_AUTH_TOKEN")
        anonymous = readie.Settings(
            router_uri=authorized.router_uri,
            timeout=authorized.timeout,
            tls=True,
            stream_logs=False,
        )
        with (
            readie.Client(anonymous) as client,
            pytest.raises(readie.PermissionDeniedError),
        ):
            readie.remote(client=client)(add)(40, 2)

    with readie.Client(authorized) as client:
        assert readie.remote(client=client)(add)(40, 2) == 42
