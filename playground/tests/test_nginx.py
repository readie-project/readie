"""The evaluation route has no token, so nginx is what keeps it off the public path."""

from pathlib import Path

import pytest

NGINX = Path(__file__).resolve().parents[2] / "nginx"


@pytest.mark.parametrize("config", ["nginx.local.conf", "nginx.prod.conf"])
def test_nginx_never_forwards_the_evaluation_route(config: str) -> None:
    text = (NGINX / config).read_text()
    block = text.index("location /playground/eval/")
    forwarded = text.index("location /playground/ {")

    assert "return 404;" in text[block:forwarded]
    # A longer prefix wins over /playground/ whatever the order, but keep it first so
    # that the reason it is there reads before the rule it overrides.
    assert block < forwarded


@pytest.mark.parametrize("config", ["nginx.local.conf", "nginx.prod.conf"])
def test_public_execution_has_no_proxy_rate_limit_and_keeps_body_and_stream_controls(
    config: str,
) -> None:
    text = (NGINX / config).read_text()
    assert "limit_req" not in text
    assert "client_max_body_size 32k;" in text
    assert "proxy_buffering off;" in text
