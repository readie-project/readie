"""The client's transport-security wiring: bearer token and TLS."""

from __future__ import annotations

import os

import grpc

from readie._channels import channel_credentials
from readie.config import Settings
from readie.transport import _auth_metadata


def test_no_token_sends_no_metadata() -> None:
    assert _auth_metadata(Settings()) is None


def test_a_token_becomes_bearer_metadata() -> None:
    os.environ["READIE_AUTH_TOKEN"] = "s3cret"  # noqa: S105 - a test literal, not a secret
    settings = Settings()
    del os.environ["READIE_AUTH_TOKEN"]

    assert _auth_metadata(settings) == [("authorization", "Bearer s3cret")]


def test_no_tls_means_a_plaintext_channel() -> None:
    assert channel_credentials(Settings(tls=False)) is None


def test_tls_produces_channel_credentials() -> None:
    assert isinstance(channel_credentials(Settings(tls=True)), grpc.ChannelCredentials)


def test_a_ca_path_implies_tls() -> None:
    # tls_ca alone turns TLS on, without needing tls=True as well.
    os.environ["READIE_TLS_CA"] = "/dev/null"
    assert Settings().use_tls
    del os.environ["READIE_TLS_CA"]
