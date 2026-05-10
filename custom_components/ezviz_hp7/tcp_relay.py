"""asyncio TCP relay that bridges HA Stream component → upstream LAN pipeline.

HA's Stream component spawns ffmpeg with whatever URL ``camera.stream_source``
returns.  We bind a TCP server on a random localhost port and have ffmpeg
connect to ``tcp://127.0.0.1:N``.  On every accepted connection we run a
fresh upstream session:

    fetch AES key (cmd 0x2001 → CAS)
    open LAN sockets, INIT/INVITE/PLAY, ECDH handshake
    pump decrypted MPEG-PS bytes back to the client socket

When the client (ffmpeg) disconnects we tear the upstream session down.  The
relay accepts connections sequentially — HA's Stream component dedup's
consumers so this is fine in practice.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .cpd7 import Cpd7LanClient, StreamDecoder

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


# Type of the callback that returns the AES-128 control key (16 ASCII bytes)
KeyFetcher = Callable[[], Awaitable[bytes]]


class CpdMpegPsRelay:
    """Local TCP server feeding MPEG-PS to ffmpeg."""

    def __init__(
        self,
        hass: HomeAssistant,
        host_provider: Callable[[], str],
        related_provider: Callable[[], str],
        get_aes_key: KeyFetcher,
        bind_host: str = "127.0.0.1",
    ) -> None:
        self._hass = hass
        self._host_provider = host_provider
        self._related_provider = related_provider
        self._get_aes_key = get_aes_key
        self._bind = bind_host
        self._server: asyncio.base_events.Server | None = None
        self._port: int = 0
        self._lock = asyncio.Lock()  # serialise concurrent upstreams

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"tcp://{self._bind}:{self._port}"

    async def async_start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, host=self._bind, port=0
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("CPD7 relay bound to no socket")
        self._port = sockets[0].getsockname()[1]
        _LOGGER.info(
            "CPD7 relay listening on %s:%d", self._bind, self._port,
        )

    async def async_stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        self._server = None
        self._port = 0

    # ── Connection handler ────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("CPD7 relay client connected: %s", peer)
        async with self._lock:
            try:
                await self._run_upstream(writer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "CPD7 relay session error for %s: %s", peer, exc
                )
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
                _LOGGER.info("CPD7 relay client disconnected: %s", peer)

    async def _run_upstream(self, writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()

        # 0. Resolve current LAN host + related sub-serial
        host = self._host_provider()
        if not host or host == "0.0.0.0":
            _LOGGER.error("CPD7 relay: doorbell LAN IP unknown")
            return
        related = self._related_provider()
        if not related:
            _LOGGER.error("CPD7 relay: related-device sub-serial unknown")
            return

        # 1. AES key
        try:
            aes_key = await self._get_aes_key()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("CPD7 relay: AES key fetch failed: %s", exc)
            return

        # 2. LAN client (sync sockets — run start/recv/close in executor)
        lan = Cpd7LanClient(
            host=host,
            related_device=related,
            aes_key=aes_key,
        )
        try:
            await loop.run_in_executor(None, lan.start)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("CPD7 relay: LAN start failed: %s", exc)
            await loop.run_in_executor(None, lan.close)
            return

        decoder = StreamDecoder(lan.ecdh_priv)

        # 3. Pump bytes
        try:
            while True:
                raw = await loop.run_in_executor(None, lan.read_chunk)
                if not raw:
                    _LOGGER.info("CPD7 relay: upstream EOF")
                    return
                decoder.feed(raw)
                plain = decoder.take()
                if not plain:
                    continue
                writer.write(plain)
                try:
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    _LOGGER.info("CPD7 relay: client closed during drain")
                    return
                except asyncio.CancelledError:
                    raise
        finally:
            try:
                await loop.run_in_executor(None, lan.close)
            except Exception:  # noqa: BLE001
                pass
