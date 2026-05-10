"""EZVIZ HP7 integration for Home Assistant."""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval

from .api import Hp7Api
from .const import (
    CONF_FEATURE_CODE,
    CONF_LIVE_VIEW_MODE,
    DEFAULT_LIVE_VIEW_MODE,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import Hp7Coordinator
from .stats import ActivityStats
from .tcp_relay import CpdMpegPsRelay

_LOGGER = logging.getLogger(__name__)


# Period between activity-summary log lines.  Short enough to spot
# trends during a beta session, long enough not to spam the log.
_STATS_SUMMARY_INTERVAL = timedelta(minutes=5)


# Refresh the AES-128 control key roughly twice within its TTL.  The
# ``fetch_lan_aes_key`` helper caches for 30 min, so a 12 min interval
# keeps the cache permanently warm without piling on cloud calls.
_AES_REFRESH_INTERVAL = timedelta(minutes=12)

# How long the relay keeps the upstream session warm after a doorbell
# event before tearing it down (in case the user never opens the
# dashboard).  Generous enough to cover a slow notification → tap →
# camera-card path.
_PREWARM_HOLD_SECONDS = 60.0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EZVIZ HP7 from a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry with credentials and device info.

    Returns:
        True if setup was successful, False otherwise.

    Raises:
        ConfigEntryNotReady: If API is not reachable.
    """
    username: str = entry.data["username"]
    password: str = entry.data["password"]
    region: str = entry.data["region"]
    serial: str = entry.data["serial"]
    token: dict[str, Any] | None = entry.data.get("token")

    # Per-install random featureCode.  Generated here for entries that
    # predate this field (migration from <0.8.3) and persisted so the
    # same value is reused on every reload — the cloud login and the
    # EUCAS ``<Sign>`` field must both carry it for HP7 / CP7 streaming
    # to work.  The token is invalidated when the value changes (the
    # cloud rejects mismatched featureCode against an old JWT).
    feature_code: str | None = entry.data.get(CONF_FEATURE_CODE)
    if not feature_code:
        feature_code = secrets.token_hex(16)
        new_data = {**entry.data, CONF_FEATURE_CODE: feature_code}
        # Drop the cached token: it was issued under a different
        # featureCode, so its ``s`` claim won't match our new value.
        new_data.pop("token", None)
        hass.config_entries.async_update_entry(entry, data=new_data)
        token = None
        _LOGGER.info(
            "[SETUP] generated per-install featureCode for entry %s "
            "(token cleared, will re-login)",
            entry.entry_id,
        )

    stats = ActivityStats()
    _LOGGER.info(
        "[SETUP] starting EZVIZ HP7 entry %s (serial=%s, region=%s)",
        entry.entry_id,
        serial,
        region,
    )

    try:
        api = Hp7Api(
            username,
            password,
            region,
            token=token,
            stats=stats,
            feature_code=feature_code,
        )
        await hass.async_add_executor_job(api.login)
        await hass.async_add_executor_job(api.detect_capabilities, serial)
    except Exception as exc:
        _LOGGER.error("Failed to connect to EZVIZ HP7 API: %s", exc)
        raise ConfigEntryNotReady(f"Cannot connect to EZVIZ HP7: {exc}") from exc

    coordinator = Hp7Coordinator(hass, entry, api, serial, stats=stats)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        _LOGGER.error("Failed to fetch initial data from coordinator: %s", exc)
        raise ConfigEntryNotReady(f"Failed to fetch EZVIZ HP7 data: {exc}") from exc

    # Resolve the camera-module sub-serial (used in <Channel RelatedDevice>)
    try:
        related = await hass.async_add_executor_job(api.get_related_device, serial)
    except Exception as exc:
        _LOGGER.debug(
            "get_related_device failed: %s — falling back to main serial", exc
        )
        related = serial

    def _host_provider() -> str:
        data = coordinator.data or {}
        return str(data.get("local_ip") or "")

    def _related_provider() -> str:
        return related

    async def _aes_key_provider() -> bytes:
        return await hass.async_add_executor_job(api.fetch_lan_aes_key, serial)

    relay: CpdMpegPsRelay | None
    try:
        relay = CpdMpegPsRelay(
            hass,
            host_provider=_host_provider,
            related_provider=_related_provider,
            get_aes_key=_aes_key_provider,
            stats=stats,
        )
        await relay.async_start()
    except Exception as exc:
        _LOGGER.warning("CPD7 relay failed to start: %s — live stream disabled", exc)
        relay = None

    live_view_mode: str = entry.options.get(CONF_LIVE_VIEW_MODE, DEFAULT_LIVE_VIEW_MODE)
    _LOGGER.info(
        "[SETUP] entry %s ready (mode=%s, relay=%s)",
        entry.entry_id,
        live_view_mode,
        relay.url if relay else "DISABLED",
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "serial": serial,
        "coordinator": coordinator,
        "relay": relay,
        "live_view_mode": live_view_mode,
        "stats": stats,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # ── Level 1: keep the AES-128 control key cached & always warm.
    # First fetch immediately (in background — don't block setup), then
    # refresh on a periodic interval shorter than the cache TTL.
    async def _refresh_aes(_now: object | None = None) -> None:
        try:
            await hass.async_add_executor_job(
                api.fetch_lan_aes_key,
                serial,
                True,  # force=True
            )
        except Exception as exc:
            _LOGGER.warning("[AES-WARMUP] periodic AES refresh failed: %s", exc)

    hass.async_create_background_task(
        _refresh_aes(),
        name="ezviz_hp7_aes_warmup",
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _refresh_aes, _AES_REFRESH_INTERVAL)
    )

    # ── Level 2: pre-warm the upstream LAN session whenever the
    # doorbell signals an event (ring, motion, smart-detection alarm).
    # By the time the user taps the notification and HA shows the
    # camera card, the session is already running and the first frame
    # appears with no extra setup latency.
    if relay is not None:
        _install_event_prewarm(hass, entry, coordinator, relay)

    # ── Periodic activity summary for log analysis ────────────────
    @callback
    def _log_stats_summary(_now: object | None = None) -> None:
        stats.log_summary()

    entry.async_on_unload(
        async_track_time_interval(hass, _log_stats_summary, _STATS_SUMMARY_INTERVAL)
    )

    return True


def _install_event_prewarm(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: Hp7Coordinator,
    relay: CpdMpegPsRelay,
) -> None:
    """Trigger ``relay.async_prewarm`` on doorbell events.

    The HP7 firmware does not populate ``Motion_Trigger`` in
    ``cam_status`` (verified empirically over hundreds of polls), so
    the only reliable per-event signal is the cloud alarm timeline —
    ``last_alarm_time`` changes whenever a smart-detection,
    intelligent-detection, doorbell-ring or gate/lock event reaches
    the cloud.  Any new value triggers a pre-warm.
    """
    state: dict[str, Any] = {
        "alarm_time": (coordinator.data or {}).get("last_alarm_time"),
    }

    @callback
    def _on_update() -> None:
        data = coordinator.data or {}
        alarm_time_now = data.get("last_alarm_time")
        alarm_name_now = data.get("alarm_name")
        if alarm_time_now is None or alarm_time_now == state["alarm_time"]:
            return
        state["alarm_time"] = alarm_time_now

        _LOGGER.info(
            "[EVENT] alarm detected (name=%s, time=%s) — pre-warming",
            alarm_name_now,
            alarm_time_now,
        )
        hass.async_create_background_task(
            relay.async_prewarm(_PREWARM_HOLD_SECONDS, trigger="alarm"),
            name="ezviz_hp7_prewarm",
        )

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: Home Assistant instance.
        entry: Config entry to unload.

    Returns:
        True if unload was successful.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        relay: CpdMpegPsRelay | None = data.get("relay")
        if relay:
            await relay.async_stop()
        api: Hp7Api | None = data.get("api")
        if api:
            api.close()
        # One last summary so the log captures what happened during the
        # entry's lifetime, useful when reviewing reloads.
        stats: ActivityStats | None = data.get("stats")
        if stats is not None:
            _LOGGER.info("[UNLOAD] final stats for entry %s:", entry.entry_id)
            stats.log_summary()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry through HA's state machine.

    Calling ``async_unload_entry`` + ``async_setup_entry`` directly leaves
    the entry in ``LOADED`` state, which makes
    ``coordinator.async_config_entry_first_refresh`` raise
    ``ConfigEntryError: ... should only be called in state
    SETUP_IN_PROGRESS`` on HA 2024.12+ — and the reload aborts with all
    entities stuck in ``unavailable``.  Delegating to
    ``hass.config_entries.async_reload`` performs the proper state
    transitions before re-running setup.
    """
    await hass.config_entries.async_reload(entry.entry_id)
