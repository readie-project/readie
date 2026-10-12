"""The playground's evaluation route, as an HTTP client."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


class EvalError(Exception):
    """The evaluation server could not be used."""


@dataclass(frozen=True, slots=True)
class RunOptions:
    """What every request asks of Readie."""

    memory: str = "8Gi"
    timeout_s: float = 300.0
    """Deadline of each step, and the budget for all the steps of one item."""
    packages: tuple[str, ...] = ()
    continue_on_error: bool = True


class EvalServer(Protocol):
    """Runs one item's steps in both lanes and returns the server's JSON."""

    def run(self, steps: list[str], options: RunOptions) -> dict[str, Any]:
        """The response of ``POST /eval/run``."""
        ...


def request_body(steps: list[str], options: RunOptions) -> dict[str, Any]:
    """The JSON the route expects."""
    return {
        "steps": steps,
        "memory": options.memory,
        "max_memory": options.memory,
        "timeout_s": options.timeout_s,
        "total_timeout_s": options.timeout_s,
        "packages": list(options.packages),
        "continue_on_error": options.continue_on_error,
    }


class HttpEvalServer:
    """Talks to ``/eval/run`` with a bearer token.

    A request covers both lanes and every step, so it can take as long as the
    item's whole budget; the HTTP timeout is that budget plus a margin. A 503 means
    another evaluation is running, and is retried a few times before giving up.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: Any = None,
        retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        import httpx  # noqa: PLC0415 - optional extra

        self._url = url.rstrip("/") + "/eval/run"
        self._headers = {"Authorization": f"Bearer {token}"}
        self._client = client or httpx.Client()
        self._http_error = httpx.HTTPError
        self._retries = retries
        self._sleep = sleep

    def run(self, steps: list[str], options: RunOptions) -> dict[str, Any]:
        """Send one item and return the parsed response."""
        body = request_body(steps, options)
        timeout = options.timeout_s + 120.0
        for attempt in range(self._retries):
            try:
                response = self._client.post(
                    self._url, json=body, headers=self._headers, timeout=timeout
                )
            except self._http_error as exc:
                msg = f"could not reach {self._url}: {exc}"
                raise EvalError(msg) from exc
            if response.status_code == 503 and attempt + 1 < self._retries:  # noqa: PLR2004
                self._sleep(min(2.0**attempt, 30.0))
                continue
            return self._parse(response)
        msg = "the server stayed busy"
        raise EvalError(msg)

    @staticmethod
    def _parse(response: Any) -> dict[str, Any]:
        if response.status_code != 200:  # noqa: PLR2004
            detail = response.text[:300]
            msg = f"the server answered {response.status_code}: {detail}"
            raise EvalError(msg)
        try:
            data = response.json()
        except ValueError as exc:
            msg = "the server's answer is not JSON"
            raise EvalError(msg) from exc
        if not isinstance(data, dict) or "lanes" not in data:
            msg = "the server's answer has no lanes"
            raise EvalError(msg)
        return data
