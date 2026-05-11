"""Phase 1.8 — coverage for ``tcp_relay.py``.

The relay's surface splits into:

* Pure buffer helpers (``_snapshot_buffer_for_new_client`` /
  ``_trim_buffer_to_last_keyframe``) — tested directly on bytes.
* State properties (``port``, ``url``, ``has_active_viewer``, ``is_warm``)
  — read off internal attributes set by hand.
* Server lifecycle (``async_start`` / ``async_stop``) — bound to a real
  ``asyncio`` loopback socket on port 0 so the test can read it back.
* Close-timer helpers — exercised by setting / inspecting
  ``_close_handle`` directly.
* ``async_prewarm`` happy / extend / failure paths — ``_spin_up_upstream``
  is mocked so we don't have to stand up a Cpd7LanClient.
* ``_spin_up_upstream`` happy + precondition failures — ``Cpd7LanClient``
  and ``StreamDecoder`` patched at the module level.
* ``_pump_upstream`` happy / EOF / writer-disconnect / error paths —
  fake LAN client + decoder driven from in-memory queues.
* ``_handle_client`` warm vs cold attach paths — fake reader / writer.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ezviz_hp7.tcp_relay import (
    _HEVC_VPS_3B,
    _HEVC_VPS_4B,
    _MPEG_PS_PACK,
    CpdMpegPsRelay,
)

RELAY_MOD = "custom_components.ezviz_hp7.tcp_relay"


# ── helpers ────────────────────────────────────────────────────────


def _relay(
    *,
    host: str = "192.0.2.10",
    related: str = "S-1-CAM",
    aes: bytes = b"K" * 16,
    stats: MagicMock | None = None,
) -> CpdMpegPsRelay:
    async def _get_key() -> bytes:
        return aes

    return CpdMpegPsRelay(
        MagicMock(),
        host_provider=lambda: host,
        related_provider=lambda: related,
        get_aes_key=_get_key,
        stats=stats,
    )


async def _cancel_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# ── pure buffer helpers ────────────────────────────────────────────


def test_snapshot_empty_buffer_returns_empty() -> None:
    assert _relay()._snapshot_buffer_for_new_client() == b""


def test_snapshot_starts_from_pack_preceding_last_vps() -> None:
    r = _relay()
    payload = (
        b"\xab" * 16 + _MPEG_PS_PACK + b"PRE_PAYLOAD_" + _HEVC_VPS_4B + b"after-vps"
    )
    r._buffer.extend(payload)
    out = r._snapshot_buffer_for_new_client()
    assert out.startswith(_MPEG_PS_PACK)
    assert _HEVC_VPS_4B in out


def test_snapshot_falls_back_to_vps_when_no_pack_precedes_it() -> None:
    r = _relay()
    r._buffer.extend(b"\xaa" * 8 + _HEVC_VPS_3B + b"tail")
    out = r._snapshot_buffer_for_new_client()
    assert out.startswith(_HEVC_VPS_3B)


def test_snapshot_returns_whole_buffer_when_no_keyframe() -> None:
    r = _relay()
    r._buffer.extend(b"no-vps-here")
    assert r._snapshot_buffer_for_new_client() == b"no-vps-here"


def test_trim_buffer_skips_when_vps_too_close_to_start() -> None:
    r = _relay()
    payload = b"\x00" * 16 + _HEVC_VPS_4B + b"rest"
    r._buffer.extend(payload)
    r._trim_buffer_to_last_keyframe()
    assert bytes(r._buffer) == payload


def test_trim_buffer_drops_bytes_before_last_keyframe() -> None:
    r = _relay()
    prefix = b"X" * 2000
    payload = prefix + _MPEG_PS_PACK + b"_packdata_" + _HEVC_VPS_4B + b"frame"
    r._buffer.extend(payload)
    r._trim_buffer_to_last_keyframe()
    out = bytes(r._buffer)
    assert out.startswith(_MPEG_PS_PACK)
    assert len(out) < len(payload)


# ── state properties ──────────────────────────────────────────────


def test_port_zero_before_start() -> None:
    assert _relay().port == 0


def test_url_combines_bind_and_port() -> None:
    r = _relay()
    r._port = 5555
    assert r.url == "tcp://127.0.0.1:5555"


def test_has_active_viewer_reflects_writer() -> None:
    r = _relay()
    assert r.has_active_viewer is False
    r._writer = MagicMock()
    assert r.has_active_viewer is True


def test_is_warm_reflects_pump_task_state() -> None:
    r = _relay()
    assert r.is_warm is False
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    r._pump_task = task
    assert r.is_warm is True
    task.done = MagicMock(return_value=True)
    assert r.is_warm is False


# ── server lifecycle ──────────────────────────────────────────────
# ``pytest-socket`` blocks real ``socket.socket()`` calls by default,
# so the tests mock ``asyncio.start_server`` rather than binding a
# real loopback port.  The server stand-in exposes the same
# attributes ``async_start`` / ``async_stop`` touch.


def _fake_server(port: int = 50500) -> MagicMock:
    sock = MagicMock()
    sock.getsockname = MagicMock(return_value=("127.0.0.1", port))
    srv = MagicMock()
    srv.sockets = [sock]
    srv.close = MagicMock()
    srv.wait_closed = AsyncMock()
    return srv


async def test_async_start_records_port_from_server_socket(mocker) -> None:
    mocker.patch(
        f"{RELAY_MOD}.asyncio.start_server",
        new_callable=AsyncMock,
        return_value=_fake_server(50500),
    )
    r = _relay()
    await r.async_start()
    assert r.port == 50500
    assert r.url == "tcp://127.0.0.1:50500"


async def test_async_start_is_idempotent(mocker) -> None:
    start = mocker.patch(
        f"{RELAY_MOD}.asyncio.start_server",
        new_callable=AsyncMock,
        return_value=_fake_server(50501),
    )
    r = _relay()
    await r.async_start()
    await r.async_start()  # second call short-circuits
    assert start.await_count == 1
    assert r.port == 50501


async def test_async_start_raises_when_server_binds_no_socket(mocker) -> None:
    srv = MagicMock()
    srv.sockets = []
    mocker.patch(
        f"{RELAY_MOD}.asyncio.start_server",
        new_callable=AsyncMock,
        return_value=srv,
    )
    with pytest.raises(RuntimeError, match="bound to no socket"):
        await _relay().async_start()


async def test_async_stop_closes_server_and_resets_port(mocker) -> None:
    srv = _fake_server(50502)
    mocker.patch(
        f"{RELAY_MOD}.asyncio.start_server",
        new_callable=AsyncMock,
        return_value=srv,
    )
    r = _relay()
    await r.async_start()
    await r.async_stop()
    srv.close.assert_called_once()
    srv.wait_closed.assert_awaited_once()
    assert r.port == 0
    assert r._server is None


async def test_async_stop_is_safe_when_never_started() -> None:
    await _relay().async_stop()  # no-op, must not raise


# ── close-timer helpers ───────────────────────────────────────────


def test_cancel_close_is_noop_without_handle() -> None:
    _relay()._cancel_close()


def test_cancel_close_cancels_pending_handle() -> None:
    r = _relay()
    h = MagicMock()
    r._close_handle = h
    r._cancel_close()
    h.cancel.assert_called_once()
    assert r._close_handle is None


async def test_reschedule_close_skips_when_seconds_non_positive() -> None:
    r = _relay()
    r._reschedule_close(0)
    assert r._close_handle is None


async def test_reschedule_close_schedules_handle() -> None:
    r = _relay()
    r._reschedule_close(60)
    assert r._close_handle is not None
    r._cancel_close()


async def test_maybe_close_upstream_skips_when_writer_attached() -> None:
    r = _relay()
    r._writer = MagicMock()
    r._close_handle = MagicMock()
    await r._maybe_close_upstream()  # writer attached → noop


async def test_maybe_close_upstream_skips_when_pump_dead() -> None:
    r = _relay()
    await r._maybe_close_upstream()  # no pump → noop


async def test_maybe_close_upstream_tears_down_when_idle(mocker) -> None:
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    td = mocker.patch.object(r, "_teardown_upstream", AsyncMock())
    await r._maybe_close_upstream()
    td.assert_awaited_once()
    await _cancel_task(pump)


# ── async_prewarm ─────────────────────────────────────────────────


async def test_prewarm_extends_window_when_already_warm() -> None:
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    stats = MagicMock(
        prewarms_triggered=0,
        prewarms_due_to_alarm=0,
        prewarms_skipped_already_warm=0,
    )
    r._stats = stats
    try:
        await r.async_prewarm(hold_seconds=30, trigger="alarm")
        assert stats.prewarms_triggered == 1
        assert stats.prewarms_due_to_alarm == 1
        assert stats.prewarms_skipped_already_warm == 1
    finally:
        r._cancel_close()
        await _cancel_task(pump)


async def test_prewarm_spins_up_and_reschedules_on_cold_start(mocker) -> None:
    r = _relay()
    stats = MagicMock(prewarms_triggered=0, prewarms_due_to_motion=0)
    r._stats = stats
    mocker.patch.object(r, "_spin_up_upstream", AsyncMock())
    try:
        await r.async_prewarm(hold_seconds=10, trigger="motion")
        assert stats.prewarms_due_to_motion == 1
        r._spin_up_upstream.assert_awaited_once()
        assert r._close_handle is not None
    finally:
        r._cancel_close()


async def test_prewarm_tears_down_when_spin_up_fails(mocker) -> None:
    r = _relay()
    stats = MagicMock(prewarms_triggered=0, errors_lan=0, lan_sessions_failed=0)
    r._stats = stats
    mocker.patch.object(
        r,
        "_spin_up_upstream",
        AsyncMock(side_effect=RuntimeError("eucas down")),
    )
    td = mocker.patch.object(r, "_teardown_upstream", AsyncMock())
    await r.async_prewarm(hold_seconds=10)
    assert stats.errors_lan == 1
    assert stats.lan_sessions_failed == 1
    td.assert_awaited_once()


# ── _spin_up_upstream ─────────────────────────────────────────────


async def test_spin_up_raises_when_host_unknown() -> None:
    r = _relay(host="")
    with pytest.raises(RuntimeError, match="LAN IP"):
        await r._spin_up_upstream()


async def test_spin_up_raises_when_host_is_zero_ip() -> None:
    r = _relay(host="0.0.0.0")
    with pytest.raises(RuntimeError, match="LAN IP"):
        await r._spin_up_upstream()


async def test_spin_up_raises_when_related_unknown() -> None:
    r = _relay(related="")
    with pytest.raises(RuntimeError, match="related"):
        await r._spin_up_upstream()


async def test_spin_up_creates_lan_client_decoder_and_pump_task(mocker) -> None:
    lan = MagicMock(ecdh_priv=MagicMock())
    lan.start = MagicMock()
    cls = mocker.patch(f"{RELAY_MOD}.Cpd7LanClient", return_value=lan)
    dec_cls = mocker.patch(f"{RELAY_MOD}.StreamDecoder", return_value=MagicMock())
    r = _relay()
    stats = MagicMock(lan_sessions_started=0)
    r._stats = stats

    await r._spin_up_upstream()

    cls.assert_called_once_with(
        host="192.0.2.10", related_device="S-1-CAM", aes_key=b"K" * 16
    )
    dec_cls.assert_called_once_with(lan.ecdh_priv)
    assert r._pump_task is not None
    assert stats.lan_sessions_started == 1
    await _cancel_task(r._pump_task)


# ── _teardown_upstream ────────────────────────────────────────────


async def test_teardown_is_noop_without_session() -> None:
    await _relay()._teardown_upstream()


async def test_teardown_cancels_pump_and_closes_lan() -> None:
    r = _relay()
    lan = MagicMock()
    r._lan = lan
    r._decoder = MagicMock()
    r._pump_task = asyncio.create_task(asyncio.sleep(60))
    r._session_started_at = time.monotonic()
    r._session_bytes = 1234
    stats = MagicMock(lan_session_total_bytes=0, lan_session_total_seconds=0.0)
    r._stats = stats

    await r._teardown_upstream()

    assert r._lan is None
    assert r._decoder is None
    assert r._pump_task is None
    assert r._buffer == b""
    lan.close.assert_called_once()


# ── _pump_upstream ────────────────────────────────────────────────


class _FakeLan:
    """Sync ``read_chunk``-backed fake matched to ``Cpd7LanClient``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read_chunk(self) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeDecoder:
    """Pushes whatever ``feed()`` receives straight back through ``take()``."""

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, raw: bytes) -> None:
        self._pending.extend(raw)

    def take(self) -> bytes:
        out = bytes(self._pending)
        self._pending.clear()
        return out


async def test_pump_upstream_returns_immediately_when_lan_missing() -> None:
    r = _relay()
    r._lan = None
    r._decoder = None
    await r._pump_upstream()  # short-circuits


async def test_pump_upstream_writes_to_buffer_and_writer() -> None:
    r = _relay()
    r._lan = _FakeLan([b"frame-A", b"frame-B"])
    r._decoder = _FakeDecoder()
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.drain = AsyncMock()
    r._writer = writer

    await r._pump_upstream()

    assert bytes(r._buffer) == b"frame-Aframe-B"
    assert r._session_bytes == len(b"frame-Aframe-B")
    writer.write.assert_any_call(b"frame-A")
    writer.write.assert_any_call(b"frame-B")


async def test_pump_upstream_drops_writer_on_broken_pipe() -> None:
    r = _relay()
    r._lan = _FakeLan([b"frame-A"])
    r._decoder = _FakeDecoder()
    writer = MagicMock()
    writer.is_closing = MagicMock(return_value=False)
    writer.drain = AsyncMock(side_effect=BrokenPipeError())
    writer.write = MagicMock()
    r._writer = writer

    await r._pump_upstream()

    assert r._writer is None


async def test_pump_upstream_counts_unexpected_errors() -> None:
    class _AngryLan:
        def read_chunk(self) -> bytes:
            raise RuntimeError("upstream blew up")

    r = _relay()
    r._lan = _AngryLan()
    r._decoder = _FakeDecoder()
    stats = MagicMock(errors_relay_pump=0)
    r._stats = stats

    await r._pump_upstream()  # swallowed in the except branch
    assert stats.errors_relay_pump == 1


async def test_pump_upstream_trims_buffer_at_keyframe(mocker) -> None:
    """Once the buffer crosses the trim target the trailing keyframe-
    aligned slice survives and everything before the most-recent VPS
    is discarded."""
    # Slash the trim target so the test runs in milliseconds.  The
    # trim helper also gates on ``last_vps >= 1024`` to avoid useless
    # work on tiny buffers, so the VPS has to sit past that offset.
    mocker.patch(f"{RELAY_MOD}._BUFFER_TRIM_TARGET", 100)
    payload = b"P" * 2000 + _MPEG_PS_PACK + b"_pkt_" + _HEVC_VPS_4B + b"tail"
    r = _relay()
    r._lan = _FakeLan([payload])
    r._decoder = _FakeDecoder()

    await r._pump_upstream()

    assert bytes(r._buffer).startswith(_MPEG_PS_PACK)
    assert _HEVC_VPS_4B in bytes(r._buffer)


# ── _handle_client ────────────────────────────────────────────────


class _EofReader:
    """``StreamReader`` stand-in that returns EOF on every read."""

    async def read(self, _n: int) -> bytes:
        return b""


async def test_handle_client_attaches_to_warm_session_and_flushes_burst(
    mocker,
) -> None:
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    r._buffer.extend(b"\xab" * 8 + _MPEG_PS_PACK + b"prefix" + _HEVC_VPS_4B + b"frame")
    # Buffer already has a keyframe, the gate must not delay the test.
    r._keyframe_seen.set()
    stats = MagicMock(relay_clients_attached=0, relay_clients_attached_warm=0)
    r._stats = stats

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50001))
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        # ``_handle_client`` schedules a close timer when the writer
        # disconnects.  Drop it so the HA test plugin doesn't fail us
        # for a lingering ``TimerHandle``.
        r._cancel_close()
        await _cancel_task(pump)

    assert stats.relay_clients_attached == 1
    assert stats.relay_clients_attached_warm == 1
    writer.write.assert_called_once()  # initial burst
    writer.close.assert_called()


async def test_handle_client_spins_up_upstream_when_cold(mocker) -> None:
    r = _relay()
    mocker.patch.object(r, "_spin_up_upstream", AsyncMock())
    # The spin_up mock doesn't actually run a pump → the keyframe event
    # would never fire on its own.  Pre-set it so the test doesn't burn
    # the full ``_KEYFRAME_WAIT_TIMEOUT_SEC`` (covered separately by
    # ``test_handle_client_attaches_after_keyframe_timeout``).
    r._keyframe_seen.set()
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50002))
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        r._cancel_close()

    r._spin_up_upstream.assert_awaited_once()


async def test_handle_client_closes_writer_on_spin_up_failure(mocker) -> None:
    r = _relay()
    stats = MagicMock(errors_lan=0, lan_sessions_failed=0)
    r._stats = stats
    mocker.patch.object(
        r,
        "_spin_up_upstream",
        AsyncMock(side_effect=RuntimeError("eucas timeout")),
    )
    mocker.patch.object(r, "_teardown_upstream", AsyncMock())
    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50003))
    writer.wait_closed = AsyncMock()

    await r._handle_client(_EofReader(), writer)

    assert stats.errors_lan == 1
    assert stats.lan_sessions_failed == 1
    writer.close.assert_called()


async def test_wait_for_client_eof_returns_on_empty_read() -> None:
    await CpdMpegPsRelay._wait_for_client_eof(_EofReader())


async def test_wait_for_client_eof_handles_reset() -> None:
    class _Reset:
        async def read(self, _n: int) -> bytes:
            raise ConnectionResetError()

    await CpdMpegPsRelay._wait_for_client_eof(_Reset())


# ── keyframe gate (HEVC VPS detection) ────────────────────────────


def test_keyframe_event_is_initially_unset() -> None:
    assert _relay()._keyframe_seen.is_set() is False


async def test_pump_sets_keyframe_event_when_vps_4b_and_full_tail_seen(
    mocker,
) -> None:
    """A bare VPS isn't enough — the gate also requires
    ``_KEYFRAME_MIN_TAIL_BYTES`` of slice data behind it."""
    mocker.patch(f"{RELAY_MOD}._KEYFRAME_MIN_TAIL_BYTES", 64)
    r = _relay()
    r._lan = _FakeLan([b"\xaa" * 8 + _HEVC_VPS_4B + b"\xbb" * 200])
    r._decoder = _FakeDecoder()
    await r._pump_upstream()
    assert r._keyframe_seen.is_set() is True


async def test_pump_sets_keyframe_event_when_vps_3b_and_full_tail_seen(
    mocker,
) -> None:
    mocker.patch(f"{RELAY_MOD}._KEYFRAME_MIN_TAIL_BYTES", 64)
    r = _relay()
    r._lan = _FakeLan([b"\xaa" * 8 + _HEVC_VPS_3B + b"\xbb" * 200])
    r._decoder = _FakeDecoder()
    await r._pump_upstream()
    assert r._keyframe_seen.is_set() is True


async def test_pump_keyframe_event_stays_clear_until_tail_fills(
    mocker,
) -> None:
    """First chunk: VPS arrives but only a few bytes of slice trail it
    — gate stays clear.  Second chunk: more slice bytes land → gate
    flips."""
    mocker.patch(f"{RELAY_MOD}._KEYFRAME_MIN_TAIL_BYTES", 1024)
    r = _relay()
    r._lan = _FakeLan(
        [
            _HEVC_VPS_4B + b"\xbb" * 100,  # bare NAL, not enough tail yet
            b"\xcc" * 2000,  # plenty of slice data → gate flips
        ]
    )
    r._decoder = _FakeDecoder()
    await r._pump_upstream()
    assert r._keyframe_seen.is_set() is True


async def test_pump_leaves_keyframe_event_clear_when_no_vps() -> None:
    r = _relay()
    r._lan = _FakeLan([b"only-deltas-no-vps-here"])
    r._decoder = _FakeDecoder()
    await r._pump_upstream()
    assert r._keyframe_seen.is_set() is False


async def test_pump_leaves_keyframe_event_clear_when_tail_too_short(
    mocker,
) -> None:
    """VPS arrives but the trailing slice bytes never reach the
    minimum required tail size — gate must stay clear."""
    mocker.patch(f"{RELAY_MOD}._KEYFRAME_MIN_TAIL_BYTES", 4096)
    r = _relay()
    r._lan = _FakeLan([_HEVC_VPS_4B + b"\xbb" * 100])  # only 100B post-VPS
    r._decoder = _FakeDecoder()
    await r._pump_upstream()
    assert r._keyframe_seen.is_set() is False


async def test_teardown_clears_keyframe_event() -> None:
    r = _relay()
    r._keyframe_seen.set()
    r._lan = MagicMock()
    r._decoder = MagicMock()
    r._pump_task = asyncio.create_task(asyncio.sleep(60))
    await r._teardown_upstream()
    assert r._keyframe_seen.is_set() is False


async def test_handle_client_waits_for_keyframe_when_burst_empty() -> None:
    """Cold-attach with empty buffer: the gate must defer the writer
    attach until the pump signals a keyframe."""
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump  # warm session, but buffer is empty
    # Event is initially unset → first attach attempt will wait.

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50050))
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    async def _trip_event_then_eof() -> None:
        # Simulate the pump task surfacing a keyframe a moment later.
        await asyncio.sleep(0.01)
        r._buffer.extend(_MPEG_PS_PACK + b"prefix" + _HEVC_VPS_4B + b"frame")
        r._keyframe_seen.set()

    helper = asyncio.create_task(_trip_event_then_eof())
    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        r._cancel_close()
        await _cancel_task(pump)
        await _cancel_task(helper)

    # The viewer received the keyframe-aligned burst (the event tripped
    # in time, so the snapshot picked up the just-arrived bytes).
    writer.write.assert_called_once()
    payload = writer.write.call_args.args[0]
    assert _HEVC_VPS_4B in payload


async def test_handle_client_attaches_after_keyframe_timeout(mocker) -> None:
    """If the pump never surfaces a keyframe inside the timeout, the
    viewer is attached anyway with an empty burst — same fallback as
    before this gate existed."""
    mocker.patch(f"{RELAY_MOD}._KEYFRAME_WAIT_TIMEOUT_SEC", 0.05)
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    # Event stays clear; ``_buffer`` stays empty → burst will be b"".

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50051))
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        r._cancel_close()
        await _cancel_task(pump)

    # No initial burst was written (buffer never gained a keyframe).
    writer.write.assert_not_called()


async def test_handle_client_skips_keyframe_wait_when_burst_available() -> None:
    """Warm attach with a keyframe already in buffer: the gate must
    NOT delay even one tick — the burst is written immediately."""
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    r._buffer.extend(_MPEG_PS_PACK + b"prefix" + _HEVC_VPS_4B + b"frame")
    # Important: leave _keyframe_seen UNSET to prove the gate is bypassed
    # when the snapshot already has a keyframe.
    assert r._keyframe_seen.is_set() is False

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50052))
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()

    t0 = time.monotonic()
    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        r._cancel_close()
        await _cancel_task(pump)
    elapsed = time.monotonic() - t0

    # No keyframe-wait timeout was hit.
    assert elapsed < 1.0
    writer.write.assert_called_once()


async def test_handle_client_attaches_writer_only_after_burst_write() -> None:
    """The pump's live-forwarding must not start until the keyframe-
    aligned initial burst has been delivered to the viewer.  Otherwise
    the viewer can see deltas before the keyframe and paint grey."""
    r = _relay()
    pump = asyncio.create_task(asyncio.sleep(60))
    r._pump_task = pump
    r._buffer.extend(_MPEG_PS_PACK + b"prefix" + _HEVC_VPS_4B + b"frame")
    r._keyframe_seen.set()

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 50053))
    writer.wait_closed = AsyncMock()
    call_order: list[str] = []

    async def _record_drain() -> None:
        call_order.append("drain")

    def _record_attach_writer() -> None:
        if r._writer is writer:
            call_order.append("writer_attached")

    writer.drain = AsyncMock(side_effect=_record_drain)
    # Sample whether the writer was attached at drain time.
    drain_attached: list[bool] = []
    original_drain = writer.drain.side_effect

    async def _check_attach_during_drain() -> None:
        drain_attached.append(r._writer is writer)
        await original_drain()

    writer.drain = AsyncMock(side_effect=_check_attach_during_drain)

    try:
        await r._handle_client(_EofReader(), writer)
    finally:
        r._cancel_close()
        await _cancel_task(pump)

    # At drain time the writer wasn't attached yet — the pump
    # couldn't have stolen the channel before the burst was flushed.
    assert drain_attached == [False]
