"""Outbound channels to workers, pooled by address.

The previous implementation opened a channel per request inside an
``async with``, so every execution paid a fresh TCP connect and HTTP/2
handshake — and the code carried a ``# TODO: Use single channel per worker``
saying as much.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

import grpc
import structlog


class WorkerChannelPool:
    """One long-lived channel per worker address.

    Channel construction is synchronous and lazily connected, so ``get`` needs
    no lock. Closing is a coroutine, though, so eviction could otherwise
    interleave with a lookup and hand out a channel that is being torn down —
    hence the lock on the mutating paths and the ``_closed`` guard.
    """

    def __init__(self, *, max_message_bytes: int) -> None:
        self._channels: dict[str, grpc.aio.Channel] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self._max_message_bytes = max_message_bytes
        self._log = structlog.get_logger("workers.channels")

    def get(self, target: str) -> grpc.aio.Channel:
        """Return the channel for an address, creating it if absent.

        Raises:
            RuntimeError: the pool has been closed.
        """
        if self._closed:
            msg = "channel pool is closed"
            raise RuntimeError(msg)

        channel = self._channels.get(target)
        if channel is None:
            channel = grpc.aio.insecure_channel(
                target,
                options=[
                    ("grpc.max_send_message_length", self._max_message_bytes),
                    ("grpc.max_receive_message_length", self._max_message_bytes),
                    # Detect a worker that vanished without closing the
                    # connection, rather than hanging on a dead channel.
                    ("grpc.keepalive_time_ms", 20_000),
                    ("grpc.keepalive_timeout_ms", 10_000),
                    ("grpc.keepalive_permit_without_calls", 1),
                ],
            )
            self._channels[target] = channel
            self._log.debug("opened worker channel", target=target)
        return channel

    def targets(self) -> list[str]:
        """Return every pooled address."""
        return list(self._channels)

    async def evict(self, target: str) -> None:
        """Close and forget one address's channel."""
        async with self._lock:
            channel = self._channels.pop(target, None)
        if channel is not None:
            await channel.close()
            self._log.debug("closed worker channel", target=target)

    async def close(self) -> None:
        """Close every channel. Idempotent."""
        async with self._lock:
            self._closed = True
            channels = list(self._channels.values())
            self._channels.clear()
        for channel in channels:
            await channel.close()

    async def __aenter__(self) -> WorkerChannelPool:
        """Enter the pool's lifetime."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close every channel on the way out."""
        await self.close()
