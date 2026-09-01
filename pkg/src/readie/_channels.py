"""Channel management.

The previous client opened a channel per call inside ``async with``, so every
execution paid a fresh TCP connection and HTTP/2 handshake and threw away the
subchannel state that makes the *next* call fast. Channels are long-lived here
and reused.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING

import grpc

if TYPE_CHECKING:
    from readie.config import Settings


def channel_options(settings: Settings) -> list[tuple[str, int]]:
    """GRPC options shared by both transports."""
    return [
        ("grpc.max_send_message_length", settings.max_message_bytes),
        ("grpc.max_receive_message_length", settings.max_message_bytes),
        # A payload can be tens of megabytes and an execution can be slow, so
        # keepalive has to outlive an idle stretch mid-call rather than tear it
        # down underneath one.
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 1),
    ]


def channel_credentials(settings: Settings) -> grpc.ChannelCredentials | None:
    """TLS credentials for the router, or ``None`` for a plaintext channel.

    A CA path verifies the router against that bundle; otherwise TLS uses the
    system trust roots. Off by default (plaintext), matching the router.
    """
    if not settings.use_tls:
        return None
    roots = Path(settings.tls_ca).read_bytes() if settings.tls_ca else None
    return grpc.ssl_channel_credentials(root_certificates=roots)


class SyncChannelCache:
    """Holds one blocking channel, created on first use."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._channel: grpc.Channel | None = None

    def get(self) -> grpc.Channel:
        """Return the channel, creating it if needed."""
        with self._lock:
            if self._channel is None:
                options = channel_options(self._settings)
                credentials = channel_credentials(self._settings)
                self._channel = (
                    grpc.secure_channel(self._settings.router_uri, credentials, options=options)
                    if credentials is not None
                    else grpc.insecure_channel(self._settings.router_uri, options=options)
                )
            return self._channel

    def close(self) -> None:
        """Close the channel, if one was ever opened. Idempotent."""
        with self._lock:
            channel, self._channel = self._channel, None
        if channel is not None:
            channel.close()


class AsyncChannelCache:
    """Holds one aio channel *per event loop*.

    grpc.aio binds a channel to the loop that created it; using it from another
    loop fails in ways that are hard to read. A client is a natural thing to
    hold in a module global and reach from more than one loop -- pytest-asyncio
    alone makes a fresh loop per test -- so the cache is keyed by loop rather
    than assuming there is only ever one.

    Entries are weakly keyed: when a loop is collected its channel goes with it,
    which is the right outcome for a loop nobody can run again.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._channels: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, grpc.aio.Channel] = (
            weakref.WeakKeyDictionary()
        )

    def get(self) -> grpc.aio.Channel:
        """Return the channel belonging to the running loop."""
        loop = asyncio.get_running_loop()
        channel = self._channels.get(loop)
        if channel is None:
            options = channel_options(self._settings)
            credentials = channel_credentials(self._settings)
            channel = (
                grpc.aio.secure_channel(self._settings.router_uri, credentials, options=options)
                if credentials is not None
                else grpc.aio.insecure_channel(self._settings.router_uri, options=options)
            )
            self._channels[loop] = channel
        return channel

    async def close(self) -> None:
        """Close the running loop's channel. Idempotent."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        channel = self._channels.pop(loop, None)
        if channel is not None:
            await channel.close(grace=None)
