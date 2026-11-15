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
