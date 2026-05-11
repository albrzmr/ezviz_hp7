"""Local TCP relay with optional pre-warmed upstream session.

The relay binds an ``asyncio.start_server`` on localhost and exposes a
URL of the form ``tcp://127.0.0.1:N``.  A consumer (HA's stream worker
for HLS, or a per-viewer ffmpeg subprocess for MJPEG) connects to it
and receives a continuous decrypted MPEG-PS stream.

Two upstream lifecycles coexist:

- **Lazy** (default): when the first client connects, the relay opens a
  fresh upstream session — fetches the AES-128 control key from EUCAS,
  runs INIT/INVITE/PLAY against the doorbell, derives the per-session
  ChaCha20 key, and starts forwarding decoded bytes.
- **Pre-warmed**: ``async_prewarm()`` opens the same upstream session
  *eagerly* (typically from a doorbell ring / motion event in
  ``__init__.py``).  The pump task accumulates decoded MPEG-PS bytes in
  a keyframe-aligned ring buffer.  When the real client connects within
  the warm window, the buffered bytes are flushed first and live bytes
  follow with no extra setup latency.

Only one upstream LAN session exists at a time — the doorbell does not
seem to like multiple concurrent ``PLAY`` sockets.  Multiple downstream
clients are not currently supported either; HA's stream component
deduplicates viewers anyway.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from .cpd7 import Cpd7LanClient, StreamDecoder

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .stats import ActivityStats

_LOGGER = logging.getLogger(__name__)


# Async callback returning the 16-byte AES-128 control key.
KeyFetcher = Callable[[], Awaitable[bytes]]


# Buffer trim threshold.  Once the warm-session ring buffer grows past
# this many bytes, we discard everything before the most recent HEVC
# keyframe (VPS NAL).  4 MB ≈ 25 s at 150 KB/s, more than enough head
# start for any realistic prewarm-to-connect window.
_BUFFER_TRIM_TARGET = 4 * 1024 * 1024

# Markers used for keyframe-aligned trimming.  Both must match the
# patterns the StreamDecoder gates on (see cpd7/decoder.py).
_HEVC_VPS_4B = b"\x00\x00\x00\x01\x40\x01"
_HEVC_VPS_3B = b"\x00\x00\x01\x40\x01"
_MPEG_PS_PACK = b"\x00\x00\x01\xba"

# How long to keep a warm session running with no client attached
# before tearing it down.  60 s comfortably covers the time between a
# doorbell event and the user opening the dashboard.
_DEFAULT_WARM_HOLD_SECONDS = 60.0

# Grace period after the last client disconnects, before we close the
# upstream session.  If a new client connects within this window
# (e.g. HA reloads its stream worker), we reuse the existing session.
_POST_CLIENT_GRACE_SECONDS = 5.0

# How long a fresh viewer waits for the pump task to surface a HEVC
# keyframe (VPS NAL) before being attached anyway.  HP7 / CP7 emit a
# VPS every ~2-4 s, so a ~6 s ceiling tolerates one missed cycle.  On
# timeout the viewer is attached mid-GOP — the same "grey for a few
# seconds" behaviour as before this gate existed.
_KEYFRAME_WAIT_TIMEOUT_SEC = 6.0

# Minimum bytes required AFTER the last VPS NAL before we declare a
# keyframe "complete" enough to flush to a new viewer.  A bare VPS is
# only a few hundred bytes; an entire IDR slice (the actual decodable
# I-frame) trails it across several decoded chunks.  Without this we
# would wake the viewer with just the NAL headers and no slice data
# — ffmpeg would receive a syntactically-valid handshake but nothing
# to decode and paint grey until the next keyframe.  32 KB comfortably
# covers a 2K HEVC I-frame on HP7 / CP7 (observed range ~30-150 KB).
_KEYFRAME_MIN_TAIL_BYTES = 32 * 1024


class CpdMpegPsRelay:
    """Localhost TCP relay with at most one active upstream LAN session."""

    def __init__(
        self,
        hass: HomeAssistant,
        host_provider: Callable[[], str],
        related_provider: Callable[[], str],
        get_aes_key: KeyFetcher,
        bind_host: str = "127.0.0.1",
        stats: ActivityStats | None = None,
    ) -> None:
        self._hass = hass
        self._host_provider = host_provider
        self._related_provider = related_provider
        self._get_aes_key = get_aes_key
        self._bind = bind_host
        self._stats = stats

        self._server: asyncio.base_events.Server | None = None
        self._port: int = 0

        # Upstream-session state.  All of these are owned by the
        # asyncio event loop; we serialise writes to them with
        # ``_state_lock`` so that ``async_prewarm`` and ``_handle_client``
        # cannot race.
        self._state_lock = asyncio.Lock()
        self._lan: Cpd7LanClient | None = None
        self._decoder: StreamDecoder | None = None
        self._pump_task: asyncio.Task | None = None
        self._buffer = bytearray()
        self._writer: asyncio.StreamWriter | None = None
        self._close_handle: asyncio.TimerHandle | None = None
        # ``_keyframe_seen`` is set by the pump the first time a HEVC
        # VPS NAL lands in ``_buffer`` after a fresh ``_spin_up_upstream``.
        # New viewers attached while the event is still clear wait for
        # it (bounded by ``_KEYFRAME_WAIT_TIMEOUT_SEC``) so they don't
        # start writing to ffmpeg mid-GOP — that would otherwise paint
        # a grey frame until the next keyframe.
        self._keyframe_seen: asyncio.Event = asyncio.Event()
        # Per-session bookkeeping for the stats summary.
        self._session_started_at: float = 0.0
        self._session_bytes: int = 0

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"tcp://{self._bind}:{self._port}"

    @property
    def has_active_viewer(self) -> bool:
        """True while a downstream client is consuming the stream.

        ``is_streaming`` for the camera entity maps to this.  Used by
        ``Hp7Camera.is_streaming`` so the entity state shows
        ``streaming`` instead of ``idle`` while the user is watching.
        """
        return self._writer is not None

    @property
    def is_warm(self) -> bool:
        """True while the upstream LAN session is alive (with or without viewer).

        Distinguishes pre-warmed sessions (relay is ready, no viewer
        attached) from full-cold (no upstream at all).
        """
        return self._is_pump_alive()

    async def async_start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._bind,
            port=0,
        )
        sockets = self._server.sockets
        if not sockets:
            raise RuntimeError("CPD7 relay bound to no socket")
        self._port = sockets[0].getsockname()[1]
        _LOGGER.info("CPD7 relay listening on %s:%d", self._bind, self._port)

    async def async_stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception as exc:
            _LOGGER.debug("server.wait_closed() raised during stop: %s", exc)
        self._server = None
        self._port = 0
        async with self._state_lock:
            await self._teardown_upstream()

    async def async_prewarm(
        self,
        hold_seconds: float = _DEFAULT_WARM_HOLD_SECONDS,
        trigger: str = "manual",
    ) -> None:
        """Open / refresh an upstream session in advance of any client.

        Called when a doorbell event suggests the user is about to open
        the live view.  If a session is already running the auto-close
        timer is just renewed.

        ``trigger`` is recorded in the stats / log for analysis (e.g.
        ``motion``, ``alarm``, ``manual``).
        """
        async with self._state_lock:
            if self._stats is not None:
                self._stats.prewarms_triggered += 1
                if trigger == "motion":
                    self._stats.prewarms_due_to_motion += 1
                elif trigger == "alarm":
                    self._stats.prewarms_due_to_alarm += 1
            if self._is_pump_alive():
                if self._stats is not None:
                    self._stats.prewarms_skipped_already_warm += 1
                _LOGGER.info(
                    "[RELAY] prewarm extension (trigger=%s, hold=%.0fs, already warm)",
                    trigger,
                    hold_seconds,
                )
                self._reschedule_close(hold_seconds)
                return
            try:
                await self._spin_up_upstream()
            except Exception as exc:
                if self._stats is not None:
                    self._stats.errors_lan += 1
                    self._stats.lan_sessions_failed += 1
                _LOGGER.warning(
                    "[RELAY] prewarm FAILED (trigger=%s): %s",
                    trigger,
                    exc,
                )
                await self._teardown_upstream()
                return
            self._reschedule_close(hold_seconds)
            _LOGGER.info(
                "[RELAY] prewarm OK (trigger=%s, hold=%.0fs)",
                trigger,
                hold_seconds,
            )

    # ── Connection handler ─────────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        client_started_at = time.monotonic()
        _LOGGER.info("[RELAY] client connected from %s", peer)
        if self._stats is not None:
            self._stats.relay_clients_attached += 1

        # Spin the upstream up (or attach to it if warm), then snapshot
        # the buffer.  The writer is NOT registered with the pump yet —
        # see the keyframe gate below.
        attached_warm = False
        initial_burst = b""
        async with self._state_lock:
            if self._is_pump_alive():
                attached_warm = True
                initial_burst = self._snapshot_buffer_for_new_client()
                if self._stats is not None:
                    self._stats.relay_clients_attached_warm += 1
            else:
                try:
                    await self._spin_up_upstream()
                except Exception as exc:
                    if self._stats is not None:
                        self._stats.errors_lan += 1
                        self._stats.lan_sessions_failed += 1
                    _LOGGER.error(
                        "[RELAY] LAN start FAILED for %s: %s",
                        peer,
                        exc,
                    )
                    await self._teardown_upstream()
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception as exc:
                        _LOGGER.debug("writer cleanup after spin-up failure: %s", exc)
                    return
            self._cancel_close()

        # If the buffer had no keyframe at attach time, wait for one
        # before opening the firehose to the viewer.  Otherwise ffmpeg
        # would start decoding mid-GOP and paint a grey frame until the
        # next VPS lands (HP7/CP7: ~2-4 s).
        if not initial_burst:
            try:
                await asyncio.wait_for(
                    self._keyframe_seen.wait(),
                    timeout=_KEYFRAME_WAIT_TIMEOUT_SEC,
                )
            except TimeoutError:
                _LOGGER.debug(
                    "[RELAY] keyframe wait timed out for %s — attaching mid-GOP",
                    peer,
                )
            async with self._state_lock:
                initial_burst = self._snapshot_buffer_for_new_client()

        _LOGGER.info(
            "[RELAY] client %s attached (warm=%s, burst=%dB)",
            peer,
            attached_warm,
            len(initial_burst),
        )

        # Send the keyframe-aligned burst FIRST, then publish the writer
        # so the pump can forward live bytes on top.  The brief window
        # between the snapshot and the attach is bounded by the trim
        # logic in ``_pump_upstream`` — any bytes that land in
        # ``_buffer`` meanwhile are P/B-frames that ffmpeg can resync
        # past once the next keyframe arrives.
        try:
            if initial_burst:
                writer.write(initial_burst)
                try:
                    await writer.drain()
                except (BrokenPipeError, ConnectionResetError):
                    _LOGGER.info("[RELAY] client closed during initial burst")
                    return
            async with self._state_lock:
                self._writer = writer
            await self._wait_for_client_eof(reader)
        finally:
            async with self._state_lock:
                if self._writer is writer:
                    self._writer = None
                    self._reschedule_close(_POST_CLIENT_GRACE_SECONDS)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as exc:
                _LOGGER.debug("writer cleanup at disconnect: %s", exc)
            duration = time.monotonic() - client_started_at
            _LOGGER.info(
                "[RELAY] client %s disconnected (duration=%.1fs, warm_attach=%s)",
                peer,
                duration,
                attached_warm,
            )

    @staticmethod
    async def _wait_for_client_eof(reader: asyncio.StreamReader) -> None:
        """Block until the client closes its end of the socket.

        ffmpeg / HA's stream worker doesn't actually send anything on
        the upstream connection — it's a one-way pipe — so ``read``
        returns ``b''`` only when the peer closes.
        """
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
        except (ConnectionResetError, BrokenPipeError):
            return
        except asyncio.CancelledError:
            raise

    # ── Upstream session lifecycle ─────────────────────────────────────────

    def _is_pump_alive(self) -> bool:
        return self._pump_task is not None and not self._pump_task.done()

    async def _spin_up_upstream(self) -> None:
        """Open a fresh LAN session and start the pump task.

        Caller must hold ``self._state_lock`` and have verified that no
        upstream is currently running.
        """
        t0 = time.monotonic()
        host = self._host_provider()
        if not host or host == "0.0.0.0":
            raise RuntimeError("doorbell LAN IP unknown")
        related = self._related_provider()
        if not related:
            raise RuntimeError("related-device sub-serial unknown")

        aes_key = await self._get_aes_key()
        lan = Cpd7LanClient(
            host=host,
            related_device=related,
            aes_key=aes_key,
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lan.start)

        self._lan = lan
        self._decoder = StreamDecoder(lan.ecdh_priv)
        self._buffer = bytearray()
        self._pump_task = asyncio.create_task(
            self._pump_upstream(),
            name="cpd7_relay_pump",
        )
        self._session_started_at = time.monotonic()
        self._session_bytes = 0
        if self._stats is not None:
            self._stats.lan_sessions_started += 1
        _LOGGER.info(
            "[RELAY] LAN upstream OPEN host=%s related=%s (setup=%.0f ms)",
            host,
            related,
            (time.monotonic() - t0) * 1000,
        )

    async def _teardown_upstream(self) -> None:
        """Cancel the pump task and close the LAN session.

        Caller must hold ``self._state_lock``.  Idempotent.
        """
        self._cancel_close()
        task = self._pump_task
        self._pump_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.debug("pump task error during teardown: %s", exc)
        lan = self._lan
        self._lan = None
        self._decoder = None
        self._buffer = bytearray()
        # Reset the keyframe gate so the next LAN session makes new
        # viewers wait for its own first VPS.
        self._keyframe_seen.clear()
        if lan is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, lan.close)
            except Exception as exc:
                _LOGGER.debug("lan.close() raised during teardown: %s", exc)
            duration = (
                time.monotonic() - self._session_started_at
                if self._session_started_at
                else 0.0
            )
            if self._stats is not None and self._session_started_at:
                self._stats.lan_session_total_seconds += duration
                self._stats.lan_session_total_bytes += self._session_bytes
            _LOGGER.info(
                "[RELAY] LAN upstream CLOSED (duration=%.1fs, bytes=%d, KB/s=%.1f)",
                duration,
                self._session_bytes,
                (self._session_bytes / 1024 / duration) if duration > 0 else 0,
            )
            self._session_started_at = 0.0
            self._session_bytes = 0

    async def _pump_upstream(self) -> None:
        """Read encrypted bytes, decrypt, push to the buffer + active writer."""
        loop = asyncio.get_running_loop()
        lan = self._lan
        decoder = self._decoder
        if lan is None or decoder is None:
            return
        try:
            while True:
                raw = await loop.run_in_executor(None, lan.read_chunk)
                if not raw:
                    _LOGGER.info("CPD7 relay: upstream EOF")
                    break
                decoder.feed(raw)
                plain = decoder.take()
                if not plain:
                    continue

                # Keep the buffer trimmed at keyframe boundaries.
                self._buffer.extend(plain)
                self._session_bytes += len(plain)
                if len(self._buffer) > _BUFFER_TRIM_TARGET:
                    self._trim_buffer_to_last_keyframe()

                # Wake any viewers waiting for the first COMPLETE
                # keyframe of this LAN session.  A bare VPS NAL is not
                # enough — the IDR slice that follows arrives across
                # several decoded chunks.  We gate on "buffer has at
                # least ``_KEYFRAME_MIN_TAIL_BYTES`` after the last
                # VPS" so the viewer wakes up with a full I-frame
                # behind it, not just headers.
                if not self._keyframe_seen.is_set():
                    buf = bytes(self._buffer)
                    last_vps = max(buf.rfind(_HEVC_VPS_4B), buf.rfind(_HEVC_VPS_3B))
                    if (
                        last_vps >= 0
                        and len(buf) - last_vps >= _KEYFRAME_MIN_TAIL_BYTES
                    ):
                        self._keyframe_seen.set()

                # If a writer is attached, forward the new chunk live.
                writer = self._writer
                if writer is not None and not writer.is_closing():
                    try:
                        writer.write(plain)
                        await writer.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        _LOGGER.info("CPD7 relay: writer closed mid-pump")
                        if self._writer is writer:
                            self._writer = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._stats is not None:
                self._stats.errors_relay_pump += 1
            _LOGGER.warning("[RELAY] pump task error: %s", exc)
        finally:
            # Allow ``_teardown_upstream`` to clean up cleanly.
            pass

    # ── Buffer helpers ─────────────────────────────────────────────────────

    def _snapshot_buffer_for_new_client(self) -> bytes:
        """Return the most recent keyframe-aligned slice of the buffer.

        Falls back to the whole buffer if no keyframe pattern is
        present (which shouldn't happen — the StreamDecoder already
        gates output on the first VPS).
        """
        if not self._buffer:
            return b""
        b = bytes(self._buffer)
        last_vps = max(b.rfind(_HEVC_VPS_4B), b.rfind(_HEVC_VPS_3B))
        if last_vps < 0:
            return b
        pack = b.rfind(_MPEG_PS_PACK, 0, last_vps)
        start = pack if pack >= 0 else last_vps
        return b[start:]

    def _trim_buffer_to_last_keyframe(self) -> None:
        b = bytes(self._buffer)
        last_vps = max(b.rfind(_HEVC_VPS_4B), b.rfind(_HEVC_VPS_3B))
        if last_vps < 1024:
            return
        pack = b.rfind(_MPEG_PS_PACK, 0, last_vps)
        start = pack if pack >= 0 else last_vps
        if start > 0:
            del self._buffer[:start]

    # ── Close-timer helpers ────────────────────────────────────────────────

    def _cancel_close(self) -> None:
        handle = self._close_handle
        self._close_handle = None
        if handle is not None:
            handle.cancel()

    def _reschedule_close(self, seconds: float) -> None:
        self._cancel_close()
        if seconds <= 0:
            return
        loop = asyncio.get_running_loop()
        self._close_handle = loop.call_later(
            seconds,
            lambda: asyncio.create_task(self._maybe_close_upstream()),
        )

    async def _maybe_close_upstream(self) -> None:
        async with self._state_lock:
            self._close_handle = None
            if self._writer is not None:
                # A client connected after the timer was scheduled.
                return
            if not self._is_pump_alive():
                return
            _LOGGER.info("CPD7 relay: warm window expired — closing upstream")
            await self._teardown_upstream()
