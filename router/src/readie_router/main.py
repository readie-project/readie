"""Process entry point."""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from readie_router import logging as log_config
from readie_router.app import App
from readie_router.config import Settings


async def _serve() -> None:
    """Build the router and run it until a signal arrives."""
    settings = Settings()
    log_config.configure(level=settings.log_level, fmt=settings.log_format)

    log = structlog.get_logger("main")
    log.info(
        "starting router",
        listen=settings.listen_addr,
        advertised=settings.advertised_addr,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Without these the 5-second graceful stop never ran: `docker stop` sends
    # SIGTERM, Python's default handler is nothing for SIGTERM under asyncio,
    # and the container was SIGKILLed after the grace period instead.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await App(settings).run(stop)


def main() -> int:
    """Run the router. Returns the process exit code."""
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:  # pragma: no cover - only from an interactive run
        return 130
    except Exception:
        structlog.get_logger("main").exception("router exited with an error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
