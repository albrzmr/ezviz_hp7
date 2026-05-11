"""Low-latency MJPEG live view via ffmpeg HEVC → JPEG transcoding.

The integration's TCP relay already exposes the decrypted MPEG-PS stream at
``tcp://127.0.0.1:N``.  When the user selects ``mjpeg`` as the live view
mode, the camera entity overrides ``handle_async_mjpeg_stream`` to spawn
ffmpeg, point it at that URL, and pipe its multipart-JPEG output straight
to the HTTP client.

This bypasses HA's HLS muxer entirely (which adds 6-9 s of buffering for
HLS segmentation) and gives sub-second glass-to-glass latency, at the
cost of one ffmpeg subprocess per active viewer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .stats import ActivityStats

_LOGGER = logging.getLogger(__name__)

# ffmpeg's mpjpeg muxer uses the literal boundary "ffmpeg" by default.
_BOUNDARY = "ffmpeg"
_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY}"

# How long to wait for ffmpeg to clean up before SIGKILLing it.
_FFMPEG_GRACE = 2.0


def _build_ffmpeg_cmd(
    upstream_url: str,
    *,
    fps: int,
    width: int,
    height: int,
    quality: int,
) -> list[str]:
    """ffmpeg invocation that:
    - reads MPEG-PS from ``upstream_url`` (our TCP relay),
    - drops audio,
    - decodes HEVC and re-encodes to motion JPEG,
    - emits multipart MJPEG (``mpjpeg`` muxer) to stdout.
    """
    # Counter-intuitively, the smallest possible probe gives the fastest
    # cold start AND the cleanest decode for this stream — measured
    # against the live HP7: probesize=32 + analyzeduration=0 hits the
    # first JPEG in ~1.2 s with zero HEVC reference errors, while a 1 MB
    # probe takes ~5 s and produces persistent ``Could not find ref``
    # warnings.  We also tell ffmpeg the input format up-front (``-f
    # mpeg``) so it doesn't try to autodetect on a tiny probe window.
    return [
        "ffmpeg",
        "-loglevel",
        "warning",
        "-f",
        "mpeg",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-fflags",
        "+discardcorrupt+nobuffer",
        "-flags",
        "low_delay",
        "-err_detect",
        "ignore_err",
        "-i",
        upstream_url,
        "-an",
        "-c:v",
        "mjpeg",
        "-q:v",
        str(quality),
        "-r",
        str(fps),
        "-vf",
        f"scale={width}:{height}",
        "-f",
        "mpjpeg",
        "pipe:1",
    ]


async def serve_mjpeg(
    request: web.Request,
    *,
    upstream_url: str,
    fps: int,
    width: int,
    height: int,
    quality: int,
    stats: ActivityStats | None = None,
) -> web.StreamResponse:
    """Stream a continuous MJPEG response back to the HTTP client.

    Spawns one ffmpeg subprocess that pulls from ``upstream_url`` (the
    integration's TCP relay) and forwards everything ffmpeg writes on
    stdout to ``request``'s response body until either side disconnects.
    """
    cmd = _build_ffmpeg_cmd(
        upstream_url,
        fps=fps,
        width=width,
        height=height,
        quality=quality,
    )
    if stats is not None:
        stats.mjpeg_sessions += 1
    started_at = time.monotonic()
    peer = request.remote
    _LOGGER.info(
        "[MJPEG] session START client=%s upstream=%s (%dx%d @ %dfps q=%d)",
        peer,
        upstream_url,
        width,
        height,
        fps,
        quality,
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": _CONTENT_TYPE,
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "close",
        },
    )
    # ``handle_async_mjpeg_stream`` is invoked from HA's CameraView, which
    # has already authorised the request — we just need to start streaming.
    await response.prepare(request)

    stderr_task = asyncio.create_task(_drain_stderr(proc))
    total_bytes = 0
    end_reason = "ok"
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                end_reason = "ffmpeg_eof"
                break
            total_bytes += len(chunk)
            try:
                await response.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError):
                # Normal: the browser/Companion closed the card.
                end_reason = "client_disconnected"
                break
    except asyncio.CancelledError:
        # Normal: HA cancelled the request handler (entry reload, server
        # shutdown, client closed). Re-raise after cleanup.
        end_reason = "cancelled"
        raise
    except Exception as exc:
        if stats is not None:
            stats.errors_mjpeg += 1
        end_reason = f"error:{type(exc).__name__}"
        _LOGGER.warning(
            "[MJPEG] unexpected session error: %s: %s", type(exc).__name__, exc
        )
        raise
    finally:
        await _terminate(proc)
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass
        try:
            await response.write_eof()
        except (ConnectionResetError, ConnectionAbortedError):
            pass
        duration = time.monotonic() - started_at
        _LOGGER.info(
            "[MJPEG] session END client=%s duration=%.1fs bytes=%d KB/s=%.1f reason=%s",
            peer,
            duration,
            total_bytes,
            (total_bytes / 1024 / duration) if duration > 0 else 0,
            end_reason,
        )
    return response


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Pull ffmpeg's stderr into the log so failures aren't silent."""
    if proc.stderr is None:
        return
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                _LOGGER.debug("ffmpeg(mjpeg): %s", text)
    except asyncio.CancelledError:
        return


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Best-effort shutdown of the ffmpeg subprocess."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_FFMPEG_GRACE)
    except TimeoutError:
        _LOGGER.debug("ffmpeg did not exit on SIGTERM, killing")
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_FFMPEG_GRACE)
        except TimeoutError:
            pass
