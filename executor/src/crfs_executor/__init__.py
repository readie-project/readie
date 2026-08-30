"""The program that runs inside a checkpoint-restore sandbox.

It pre-imports a planned set of modules, announces that it is worth
checkpointing, and then serves execution requests from the worker over a unix
socket: unpickle a call, make it, pickle the result back.

Nothing here is imported by the router, the worker or the client SDK. The
coupling runs through the wire protocol in ``protocol.py``, whose Go 
counterpart is ``worker/internal/executor``.
"""

from __future__ import annotations

from crfs_executor.config import ConfigError, Settings
from crfs_executor.protocol import ProtocolError

__version__ = "0.1.0"
__all__ = ["ConfigError", "ProtocolError", "Settings", "__version__"]
