"""Phase 1.9 — coverage for ``mjpeg.py``.

Two surfaces:

* ``_build_ffmpeg_cmd`` — pure function, asserts the low-latency flags
  that make the cold start sub-1.2 s on the live HP7 (smallest probe,
  ``low_delay``, ``-an`` to drop audio, scale filter, etc.).
* ``serve_mjpeg`` — spawns ffmpeg, pumps stdout to an aiohttp response,
  classifies the END line (``ok`` / ``ffmpeg_eof`` / ``client_disconnected``
  / ``cancelled`` / ``error:*``) and tears the subprocess down.

``asyncio.create_subprocess_exec`` is patched at module level so no
real ffmpeg ever runs.  The aiohttp response is a ``MagicMock`` with
async-callable ``prepare`` / ``write`` / ``write_eof``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.ezviz_hp7.mjpeg import (
    _build_ffmpeg_cmd,
    _drain_stderr,
    _terminate,
    serve_mjpeg,
)

MJPEG_MOD = "custom_components.ezviz_hp7.mjpeg"


# ── helpers ────────────────────────────────────────────────────────


class _FakeReader:
    """Async stream stand-in for ``proc.stdout`` / ``proc.stderr``."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    async def readline(self) -> bytes:
        return await self.read()


class _FakeProc:
    def __init__(
        self,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        returncode: int | None = None,
    ) -> None:
        self.stdout = _FakeReader(stdout or [])
        self.stderr = _FakeReader(stderr or [])
        self.returncode = returncode
        self.terminate = MagicMock()
        self.kill = MagicMock()
        self.wait = AsyncMock(return_value=returncode if returncode is not None else 0)


def _request() -> MagicMock:
    req = MagicMock()
    req.remote = "192.0.2.5"
    return req


def _fake_response() -> MagicMock:
    r = MagicMock()
    r.prepare = AsyncMock()
    r.write = AsyncMock()
    r.write_eof = AsyncMock()
    return r


# ── _build_ffmpeg_cmd ──────────────────────────────────────────────


def test_build_ffmpeg_cmd_contains_low_latency_flags() -> None:
    cmd = _build_ffmpeg_cmd(
        "tcp://127.0.0.1:9999", fps=8, width=1280, height=720, quality=5
    )
    # Specific flags the cold-start tuning depends on.
    for token in (
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-flags",
        "low_delay",
        "-an",  # drop audio
        "-c:v",
        "mjpeg",
        "-f",
        "mpjpeg",
    ):
        assert token in cmd, f"missing token {token!r} in cmd: {cmd}"


def test_build_ffmpeg_cmd_passes_input_and_scaler() -> None:
    cmd = _build_ffmpeg_cmd(
        "tcp://relay:5555", fps=15, width=640, height=360, quality=8
    )
    assert "tcp://relay:5555" in cmd
    assert "scale=640:360" in cmd
    assert str(15) in cmd  # -r 15
    assert "8" in cmd  # -q:v 8


# ── serve_mjpeg flow ──────────────────────────────────────────────


@pytest.fixture
def patch_proc(mocker):
    """Patch ``asyncio.create_subprocess_exec`` and ``web.StreamResponse``."""
    created: dict[str, Any] = {}

    def _factory(stdout: list[bytes] | None = None, **kw: Any):
        async def _spawn(*_a: Any, **_kw: Any) -> _FakeProc:
            proc = _FakeProc(stdout=stdout, **kw)
            created["proc"] = proc
            return proc

        return _spawn

    response = _fake_response()
    mocker.patch(f"{MJPEG_MOD}.web.StreamResponse", return_value=response)
    created["response"] = response
    created["_factory"] = _factory
    created["mocker"] = mocker
    return created


def _arm(patch_proc: dict, stdout: list[bytes]) -> None:
    """Helper: wire the subprocess factory with a specific stdout payload."""
    patch_proc["mocker"].patch(
        f"{MJPEG_MOD}.asyncio.create_subprocess_exec",
        side_effect=patch_proc["_factory"](stdout=stdout),
    )


async def test_serve_mjpeg_streams_stdout_to_response_until_eof(
    patch_proc: dict,
) -> None:
    _arm(patch_proc, [b"chunk1-", b"chunk2-end"])
    stats = MagicMock(mjpeg_sessions=0)

    out = await serve_mjpeg(
        _request(),
        upstream_url="tcp://relay:5555",
        fps=8,
        width=320,
        height=180,
        quality=10,
        stats=stats,
    )

    assert out is patch_proc["response"]
    patch_proc["response"].prepare.assert_awaited_once()
    patch_proc["response"].write.assert_any_await(b"chunk1-")
    patch_proc["response"].write.assert_any_await(b"chunk2-end")
    patch_proc["response"].write_eof.assert_awaited_once()
    assert stats.mjpeg_sessions == 1


async def test_serve_mjpeg_classifies_client_disconnect_as_clean_exit(
    patch_proc: dict,
) -> None:
    _arm(patch_proc, [b"chunk-A", b"chunk-B"])
    patch_proc["response"].write = AsyncMock(side_effect=ConnectionResetError())
    stats = MagicMock(mjpeg_sessions=0, errors_mjpeg=0)

    await serve_mjpeg(
        _request(),
        upstream_url="tcp://relay:5555",
        fps=8,
        width=320,
        height=180,
        quality=10,
        stats=stats,
    )
    # Client-side reset is treated as a normal end — no error stat bump.
    assert stats.errors_mjpeg == 0


async def test_serve_mjpeg_propagates_cancellation_and_cleans_up(
    patch_proc: dict,
) -> None:
    _arm(patch_proc, [b"x" * 10])
    patch_proc["response"].write = AsyncMock(side_effect=asyncio.CancelledError())
    stats = MagicMock(mjpeg_sessions=0, errors_mjpeg=0)

    with pytest.raises(asyncio.CancelledError):
        await serve_mjpeg(
            _request(),
            upstream_url="tcp://relay:5555",
            fps=8,
            width=320,
            height=180,
            quality=10,
            stats=stats,
        )
    # CancelledError is HA's signal, NOT an error condition.
    assert stats.errors_mjpeg == 0
    patch_proc["response"].write_eof.assert_awaited_once()


async def test_serve_mjpeg_counts_unexpected_errors(patch_proc: dict) -> None:
    _arm(patch_proc, [b"data"])
    patch_proc["response"].write = AsyncMock(side_effect=RuntimeError("boom"))
    stats = MagicMock(mjpeg_sessions=0, errors_mjpeg=0)

    with pytest.raises(RuntimeError, match="boom"):
        await serve_mjpeg(
            _request(),
            upstream_url="tcp://relay:5555",
            fps=8,
            width=320,
            height=180,
            quality=10,
            stats=stats,
        )
    assert stats.errors_mjpeg == 1


async def test_serve_mjpeg_tolerates_missing_stats_object(patch_proc: dict) -> None:
    _arm(patch_proc, [b"a", b"b"])
    out = await serve_mjpeg(
        _request(),
        upstream_url="tcp://relay:5555",
        fps=8,
        width=320,
        height=180,
        quality=10,
        stats=None,
    )
    assert out is patch_proc["response"]


# ── _drain_stderr ──────────────────────────────────────────────────


async def test_drain_stderr_logs_until_eof(caplog) -> None:
    proc = _FakeProc(stderr=[b"warn1\n", b"warn2\n"])
    with caplog.at_level("DEBUG"):
        await _drain_stderr(proc)
    text = " ".join(r.message for r in caplog.records)
    assert "warn1" in text and "warn2" in text


async def test_drain_stderr_returns_when_stderr_is_none() -> None:
    proc = _FakeProc()
    proc.stderr = None  # type: ignore[assignment]
    await _drain_stderr(proc)  # must not raise


async def test_drain_stderr_handles_cancellation() -> None:
    """``_drain_stderr`` returns silently when its task is cancelled
    (the finally block in ``serve_mjpeg`` cancels it on shutdown)."""

    class _CancellingReader:
        async def readline(self) -> bytes:
            raise asyncio.CancelledError()

    proc = _FakeProc()
    proc.stderr = _CancellingReader()  # type: ignore[assignment]
    await _drain_stderr(proc)  # must NOT propagate


# ── _terminate ─────────────────────────────────────────────────────


async def test_terminate_is_noop_when_already_exited() -> None:
    proc = _FakeProc(returncode=0)
    await _terminate(proc)
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


async def test_terminate_sigterm_happy_path() -> None:
    proc = _FakeProc(returncode=None)
    proc.wait = AsyncMock(return_value=0)
    await _terminate(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_not_called()


async def test_terminate_escalates_to_sigkill_on_timeout(mocker) -> None:
    proc = _FakeProc(returncode=None)
    # ``proc.wait`` is invoked synchronously to build the awaitable
    # passed to ``wait_for`` — return a sentinel so the mocked
    # ``wait_for`` never has to await a real coroutine.
    proc.wait = MagicMock(return_value=MagicMock())
    mocker.patch(
        f"{MJPEG_MOD}.asyncio.wait_for",
        new_callable=AsyncMock,
        side_effect=[TimeoutError(), None],
    )
    await _terminate(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


async def test_terminate_swallows_processlookup_on_terminate() -> None:
    """Race: process exits between ``returncode is None`` and ``terminate()``."""
    proc = _FakeProc(returncode=None)
    proc.terminate = MagicMock(side_effect=ProcessLookupError())
    await _terminate(proc)  # must not raise
    proc.kill.assert_not_called()


async def test_terminate_swallows_processlookup_on_kill(mocker) -> None:
    proc = _FakeProc(returncode=None)
    proc.wait = MagicMock(return_value=MagicMock())
    proc.kill = MagicMock(side_effect=ProcessLookupError())
    mocker.patch(
        f"{MJPEG_MOD}.asyncio.wait_for",
        new_callable=AsyncMock,
        side_effect=[TimeoutError(), TimeoutError()],
    )
    await _terminate(proc)  # must not raise even when SIGKILL fails
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
