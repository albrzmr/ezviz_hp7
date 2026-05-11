"""Lightweight activity counters for the EZVIZ HP7 integration.

The goal is to give the maintainer visibility into how many cloud /
EUCAS / LAN calls the integration makes during real use, so we can
spot anything that might trigger EZVIZ-side rate limits or account
flags before the integration is opened up to the wider community.

Each ``ActivityStats`` instance lives on ``hass.data[DOMAIN][entry]``.
Hot-path code paths (``api.py``, ``tcp_relay.py``, ``mjpeg.py``…)
bump the relevant counter; a periodic background task in
``__init__.py`` logs the running totals every few minutes so they end
up in ``home-assistant.log`` ready for ``grep`` / ``wc -l``.

All counters are simple ``int`` fields — no synchronisation needed
because everything runs on the asyncio event loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field

_LOGGER = logging.getLogger(__name__)


@dataclass
class ActivityStats:
    """Running totals for the lifetime of the integration."""

    started_at: float = field(default_factory=time.time)

    # ── Cloud / EUCAS ─────────────────────────────────────────────────
    cloud_logins: int = 0  # initial logins (no cached token)
    cloud_relogins: int = 0  # forced re-logins after a CAS error
    cloud_polls: int = 0  # coordinator ticks (1 per UPDATE_INTERVAL_SEC)
    # Phase 6.2 split: the coordinator hits two cloud endpoints at
    # different cadences.  ``cloud_polls_alarms`` increments every
    # tick (alarms must be fast), ``cloud_polls_static`` only every
    # ``STATUS_POLL_INTERVAL_SEC``.  The ratio reflects the actual
    # HTTP footprint; ``cloud_polls`` stays as the tick counter for
    # backwards-compat with prior log scraping.
    cloud_polls_alarms: int = 0
    cloud_polls_static: int = 0

    aes_cache_hits: int = 0  # ``fetch_lan_aes_key`` served from cache
    aes_cache_misses: int = 0  # ``fetch_lan_aes_key`` had to call EUCAS
    aes_force_refreshes: int = 0  # ``force=True`` (background warm-up etc.)
    aes_invalidations: int = 0  # cache cleared (probable re-pairing)

    # ── LAN / streaming ──────────────────────────────────────────────
    lan_sessions_started: int = 0
    lan_sessions_failed: int = 0
    lan_session_total_bytes: int = 0
    lan_session_total_seconds: float = 0.0

    relay_clients_attached: int = 0
    relay_clients_attached_warm: int = 0  # subset of above that hit a warm session

    mjpeg_sessions: int = 0
    hls_sessions: int = 0

    # ── Pre-warm ─────────────────────────────────────────────────────
    prewarms_triggered: int = 0
    prewarms_due_to_motion: int = 0
    prewarms_due_to_alarm: int = 0
    prewarms_skipped_already_warm: int = 0

    # ── Errors ───────────────────────────────────────────────────────
    errors_cas: int = 0
    errors_lan: int = 0
    errors_mjpeg: int = 0
    errors_relay_pump: int = 0

    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def log_summary(self) -> None:
        """Emit a single INFO line with all non-zero counters.

        Designed for ``grep`` after the fact — each call writes a line
        prefixed with ``EZVIZ HP7 stats``.
        """
        snap = asdict(self)
        uptime = snap.pop("started_at")
        # Only show counters that actually moved (keeps the line short).
        nonzero = {
            k: v
            for k, v in snap.items()
            if (isinstance(v, (int, float)) and v) or not isinstance(v, (int, float))
        }
        bytes_total = nonzero.pop("lan_session_total_bytes", 0)
        secs_total = nonzero.pop("lan_session_total_seconds", 0.0)
        avg = (bytes_total / secs_total) if secs_total > 0 else 0.0
        nonzero["lan_total_MB"] = round(bytes_total / (1024 * 1024), 2)
        nonzero["lan_total_seconds"] = round(secs_total, 1)
        nonzero["lan_avg_KBps"] = round(avg / 1024, 1)
        _LOGGER.info(
            "EZVIZ HP7 stats (uptime=%.0fs since %s): %s",
            time.time() - uptime,
            time.strftime("%H:%M:%S", time.localtime(uptime)),
            nonzero,
        )
