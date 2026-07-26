"""The program that runs inside a checkpoint-restore sandbox.

It pre-imports a planned set of modules, announces that it is worth
checkpointing, and then serves execution requests from the worker over a unix
socket: unpickle a call, make it, pickle the result back.

Nothing here is imported by the router, the worker or the client SDK. The
coupling runs through two contracts instead — the wire protocol in
``protocol.py``, whose Go counterpart is ``worker/internal/executor``, and the
ready sentinel the offline pipeline greps for.
"""

from __future__ import annotations

from crfs_executor.config import READY_SENTINEL, ConfigError, Settings
from crfs_executor.protocol import ProtocolError

__version__ = "0.1.0"
__all__ = ["READY_SENTINEL", "ConfigError", "ProtocolError", "Settings", "__version__"]
