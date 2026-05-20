"""Phase 6.2 — coverage for ``coordinator.py``.

The coordinator polls two endpoints at independent cadences:

* alarms (``unifiedmsg/list``) every tick — must always be fresh.
* static device info (``pagelist``) every
  ``STATUS_POLL_INTERVAL_SEC`` — cached between refreshes.

Tests here exercise:

- First tick (cold cache) hits both endpoints.
- Subsequent tick within the static window hits only the alarm.
- A static refresh after the window hits both.
- A transient static failure with a warm cache logs at debug and
  keeps the dashboard alive on cached data.
- A static failure with a cold cache surfaces as ``UpdateFailed`` so
  HA defers setup with ``ConfigEntryNotReady``.
- An alarm failure always surfaces as ``UpdateFailed`` regardless of
  cache state — the dashboard would otherwise show stale alarm state.
- Stats counters track ticks, alarm hits and static hits separately.
"""

from __future__ import annotations

import time
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.ezviz_hp7.const import (
    STATUS_POLL_INTERVAL_SEC,
    UPDATE_INTERVAL_SEC,
)
from custom_components.ezviz_hp7.coordinator import Hp7Coordinator
from custom_components.ezviz_hp7.stats import ActivityStats


def _api(
    *,
    alarms: dict | None = None,
    static: dict | None = None,
    alarms_error: Exception | None = None,
    static_error: Exception | None = None,
) -> MagicMock:
    """Mock ``Hp7Api`` exposing the two split methods."""
    api = MagicMock()
    if alarms_error is not None:
        api.get_alarms.side_effect = alarms_error
    else:
        api.get_alarms.return_value = alarms or {
            "last_alarm_time": "2026-05-11 10:00:00",
            "last_alarm_pic": "https://x/snap.jpg",
            "alarm_name": "Smart Detection Alarm",
            "seconds_last_trigger": 3,
        }
    if static_error is not None:
        api.get_static_status.side_effect = static_error
    else:
        api.get_static_status.return_value = static or {
            "name": "Doorbell",
            "version": "V5.3.6",
            "status": 1,
            "local_ip": "192.0.2.10",
            "signal": 80,
        }
    return api


@pytest.fixture
def coord(hass, mock_config_entry):
    """Build a ``Hp7Coordinator`` bound to the real HA test runtime."""

    def _make(
        *,
        api: MagicMock | None = None,
        stats: ActivityStats | None = None,
        cached_static: dict | None = None,
        last_static_fetch: float | None = None,
    ) -> Hp7Coordinator:
        c = Hp7Coordinator(
            hass,
            mock_config_entry,
            api or _api(),
            "SER",
            stats=stats,
        )
        if cached_static is not None:
            c._cached_static = dict(cached_static)
        # ``None`` (the default) keeps whatever ``Hp7Coordinator.__init__``
        # set — i.e. "never fetched" — so the first-tick / cold-cache
        # branches behave identically on machines with low monotonic-clock
        # values (fresh CI runners) and high ones (a developer laptop).
        if last_static_fetch is not None:
            c._last_static_fetch = last_static_fetch
        return c

    return _make


# ── First tick: cold cache → both endpoints ────────────────────────


async def test_first_tick_fetches_both_endpoints(coord) -> None:
    api = _api()
    c = coord(api=api)
    out = await c._async_update_data()

    api.get_alarms.assert_called_once_with("SER")
    api.get_static_status.assert_called_once_with("SER")
    # Merged dict carries both halves.
    assert out["alarm_name"] == "Smart Detection Alarm"
    assert out["name"] == "Doorbell"


async def test_first_tick_caches_static(coord) -> None:
    api = _api(static={"name": "Doorbell", "status": 1})
    c = coord(api=api)
    await c._async_update_data()

    assert c._cached_static == {"name": "Doorbell", "status": 1}
    assert c._last_static_fetch > 0.0


# ── Subsequent tick within the static window → alarms only ─────────


async def test_subsequent_tick_within_window_skips_static(coord) -> None:
    api = _api()
    c = coord(
        api=api,
        cached_static={"name": "Cached", "status": 1},
        last_static_fetch=time.monotonic(),
    )
    out = await c._async_update_data()

    api.get_alarms.assert_called_once_with("SER")
    api.get_static_status.assert_not_called()
    # Output merges fresh alarms with the cached static dict.
    assert out["name"] == "Cached"
    assert out["alarm_name"] == "Smart Detection Alarm"


# ── Tick after the static window → both endpoints ──────────────────


async def test_tick_after_window_refetches_static(coord) -> None:
    api = _api(static={"name": "Fresh", "status": 1})
    stale_fetch_time = time.monotonic() - (STATUS_POLL_INTERVAL_SEC + 1)
    c = coord(
        api=api,
        cached_static={"name": "Old"},
        last_static_fetch=stale_fetch_time,
    )
    out = await c._async_update_data()

    api.get_static_status.assert_called_once_with("SER")
    assert out["name"] == "Fresh"


# ── Static failure with warm cache → reuse, don't propagate ────────


async def test_static_failure_with_warm_cache_reuses_cached_data(coord, caplog) -> None:
    api = _api(static_error=RuntimeError("EUCAS hiccup"))
    stale = time.monotonic() - (STATUS_POLL_INTERVAL_SEC + 1)
    c = coord(
        api=api,
        cached_static={"name": "Old", "status": 1, "signal": 70},
        last_static_fetch=stale,
    )
    out = await c._async_update_data()

    # Output preserves the cached static fields and merges fresh alarms.
    assert out["name"] == "Old"
    assert out["status"] == 1
    assert out["alarm_name"] == "Smart Detection Alarm"
    assert any("Static poll failed" in r.message for r in caplog.records)


# ── Static failure with cold cache → UpdateFailed ──────────────────


async def test_static_failure_with_cold_cache_raises_update_failed(coord) -> None:
    api = _api(static_error=RuntimeError("cloud down"))
    c = coord(api=api)  # cached_static defaults to {}

    with pytest.raises(UpdateFailed, match="initial static poll failed"):
        await c._async_update_data()


# ── Alarm failure → always UpdateFailed ────────────────────────────


async def test_alarm_failure_raises_update_failed(coord) -> None:
    api = _api(alarms_error=RuntimeError("alarm endpoint down"))
    c = coord(api=api)

    with pytest.raises(UpdateFailed, match="alarm poll failed"):
        await c._async_update_data()


async def test_alarm_failure_skips_static_fetch(coord) -> None:
    """Alarm failure bails out before reaching the static fetch."""
    api = _api(alarms_error=RuntimeError("alarm endpoint down"))
    c = coord(api=api)
    with pytest.raises(UpdateFailed):
        await c._async_update_data()
    api.get_static_status.assert_not_called()


# ── Stats counters ─────────────────────────────────────────────────


async def test_stats_increment_on_full_tick(coord) -> None:
    stats = ActivityStats()
    c = coord(api=_api(), stats=stats)
    await c._async_update_data()

    assert stats.cloud_polls == 1
    assert stats.cloud_polls_alarms == 1
    assert stats.cloud_polls_static == 1


async def test_stats_increment_only_alarms_on_cached_tick(coord) -> None:
    stats = ActivityStats()
    c = coord(
        api=_api(),
        stats=stats,
        cached_static={"name": "Cached"},
        last_static_fetch=time.monotonic(),
    )
    await c._async_update_data()

    assert stats.cloud_polls == 1
    assert stats.cloud_polls_alarms == 1
    assert stats.cloud_polls_static == 0


async def test_stats_do_not_increment_static_on_failure(coord) -> None:
    stats = ActivityStats()
    api = _api(static_error=RuntimeError("blip"))
    stale = time.monotonic() - (STATUS_POLL_INTERVAL_SEC + 1)
    c = coord(
        api=api,
        stats=stats,
        cached_static={"name": "Old"},
        last_static_fetch=stale,
    )
    await c._async_update_data()

    assert stats.cloud_polls == 1
    assert stats.cloud_polls_alarms == 1
    # No bump when the call raised — even though it was attempted.
    assert stats.cloud_polls_static == 0


async def test_stats_skipped_when_no_stats_object(coord) -> None:
    """``stats=None`` must not blow up the coordinator."""
    c = coord(api=_api(), stats=None)
    await c._async_update_data()  # no raise


# ── Diagnostic INFO log on new alarms (issue #8) ───────────────────


async def test_logs_info_once_per_new_alarm_time(coord, caplog) -> None:
    """The coordinator must emit a single INFO log per new
    ``last_alarm_time`` so the user can read the raw ``alarmType`` code
    and report it back — that's how non-English EZVIZ accounts unblock
    the cloud-only sensors (smart / intelligent / doorbell ringing)."""
    api = _api(
        alarms={
            "last_alarm_time": "2026-05-20 12:00:00",
            "alarm_name": "L'app EZVIZ apre il cancello",
            "alarm_type_code": "3001",
        }
    )
    c = coord(api=api)

    caplog.set_level("INFO")
    await c._async_update_data()
    info_lines = [r for r in caplog.records if "EZVIZ alarm received" in r.message]
    assert len(info_lines) == 1
    msg = info_lines[0].getMessage()
    assert "code=3001" in msg
    assert "3001" in msg and "cancello" in msg


async def test_logs_info_skipped_on_repeat_alarm_time(coord, caplog) -> None:
    api = _api(
        alarms={
            "last_alarm_time": "2026-05-20 12:00:00",
            "alarm_name": "Ring",
            "alarm_type_code": "3001",
        }
    )
    c = coord(api=api)
    caplog.set_level("INFO")
    await c._async_update_data()
    await c._async_update_data()
    info_lines = [r for r in caplog.records if "EZVIZ alarm received" in r.message]
    assert len(info_lines) == 1


# ── Configuration ──────────────────────────────────────────────────


async def test_update_interval_matches_const(coord) -> None:
    c = coord(api=_api())
    assert c.update_interval == timedelta(seconds=UPDATE_INTERVAL_SEC)
