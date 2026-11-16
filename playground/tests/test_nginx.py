"""Public proxy configuration keeps its rate limit, body and streaming controls."""

from pathlib import Path

import pytest

NGINX = Path(__file__).resolve().parents[2] / "nginx"


@pytest.mark.parametrize("config", ["nginx.local.conf", "nginx.prod.conf"])
def test_public_execution_keeps_rate_limit_body_and_stream_controls(
    config: str,
) -> None:
    text = (NGINX / config).read_text()
    assert "limit_req zone=playground" in text
    assert "client_max_body_size 32k;" in text
    assert "proxy_buffering off;" in text


def _server_block(text: str, server_name: str) -> str:
    """The text from one server_name directive to the next server block, or the file end."""
    start = text.index(f"server_name {server_name}")
    end = text.find("server {", start)
    return text[start:] if end == -1 else text[start:end]


def test_run_hostname_serves_only_the_root_endpoint_and_healthz() -> None:
    text = (NGINX / "nginx.prod.conf").read_text()
    block = _server_block(text, "run.readie.org")
    assert "location = / {" in block
    assert "location = /healthz {" in block
    assert "grpc_pass" not in block
    assert "location / {\n            return 404;" in block
    # The root path is the endpoint, and the service route is /run.
    assert "proxy_pass http://playground/run;" in block
    assert "limit_req zone=playground" in block
    assert "client_max_body_size 32k;" in block
    assert "proxy_buffering off;" in block


def test_router_block_serves_only_the_api_hostname() -> None:
    text = (NGINX / "nginx.prod.conf").read_text()
    assert "server_name api.readie.org;" in text
