"""Data update coordinator for EZVIZ HP7.

Phase 6.2 split — the coordinator now polls two cloud endpoints at
independent cadences:

* alarms (``unifiedmsg/list``) every tick — latency-sensitive,
  drives doorbell-ring / motion binary sensors and the
  ``_install_event_prewarm`` listener.
* static device info (``pagelist``) every
  ``STATUS_POLL_INTERVAL_SEC`` — covers WiFi signal, firmware,
  device status / IP, all of which change slowly.

The merged dict consumed by entities is always a fresh-alarms +
last-known-static composition, so a transient static-poll failure
keeps the dashboard usable while the alarms continue to flow.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import STATUS_POLL_INTERVAL_SEC, UPDATE_INTERVAL_SEC

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import Hp7Api
    from .stats import ActivityStats

_LOGGER = logging.getLogger(__name__)


class Hp7Coordinator(DataUpdateCoordinator):
    """Periodic device-status poller for the EZVIZ HP7 / CP7."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: Hp7Api,
        serial: str,
        stats: ActivityStats | None = None,
    ) -> None:
        """Initialize the coordinator.

        ``config_entry`` is required by HA 2024.12+ — without it
        ``async_config_entry_first_refresh`` raises ``ConfigEntryError``
        ("Detected code that uses async_config_entry_first_refresh,
        which is only supported for coordinators with a config entry").
        """
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="EZVIZ HP7",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SEC),
        )
        self.api = api
        self.serial = serial
        self._stats = stats
        # Static-poll bookkeeping (cadence split).  ``_last_static_fetch``
        # uses ``None`` as the "never fetched" sentinel rather than ``0.0``
        # — ``time.monotonic()`` has an undefined origin (the system
        # uptime on Linux, the host uptime on macOS, etc.), so a sentinel
        # of ``0.0`` would falsely look "recent" on freshly-booted Linux
        # boxes whose monotonic clock is still in the seconds range.
        self._cached_static: dict[str, Any] = {}
        self._last_static_fetch: float | None = None
        # Tracks the most recent ``last_alarm_time`` we emitted a
        # diagnostic INFO log for.  Used to surface the raw cloud
        # alarm code / name exactly once per new event so users can
        # report them — the ``alarmType`` numeric code is language-
        # stable and is what binary sensors should match on long-term
        # (issue #8).
        self._last_logged_alarm_time: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch fresh alarms + (occasionally) refresh static device info.

        Failure modes:

        * Alarms fetch fails → wrap in ``UpdateFailed`` so HA marks
          entities unavailable.  The dashboard would otherwise show a
          stale alarm state forever.
        * Static fetch fails → keep using the cached static dict and
          log at debug.  If the cache is still empty (first tick),
          fall back to wrapping in ``UpdateFailed`` so the entry
          surface as ``ConfigEntryNotReady`` instead of registering
          entities with half-empty data.
        """
        if self._stats is not None:
            self._stats.cloud_polls += 1

        try:
            alarms = await self.hass.async_add_executor_job(
                self.api.get_alarms,
                self.serial,
            )
        except Exception as exc:
            raise UpdateFailed(f"EZVIZ HP7 alarm poll failed: {exc}") from exc
        if self._stats is not None:
            self._stats.cloud_polls_alarms += 1

        now = time.monotonic()
        if (
            self._last_static_fetch is None
            or now - self._last_static_fetch >= STATUS_POLL_INTERVAL_SEC
        ):
            try:
                self._cached_static = await self.hass.async_add_executor_job(
                    self.api.get_static_status,
                    self.serial,
                )
                self._last_static_fetch = now
                if self._stats is not None:
                    self._stats.cloud_polls_static += 1
            except Exception as exc:
                # Tolerate transient failures once the cache is warm.
                # On the very first tick the cache is empty and we
                # MUST surface the failure so HA retries ``setup_entry``.
                if not self._cached_static:
                    raise UpdateFailed(
                        f"EZVIZ HP7 initial static poll failed: {exc}"
                    ) from exc
                _LOGGER.debug(
                    "Static poll failed (%s) — reusing cached static data", exc
                )

        merged = {**self._cached_static, **alarms}
        new_alarm_time = merged.get("last_alarm_time")
        if new_alarm_time and new_alarm_time != self._last_logged_alarm_time:
            self._last_logged_alarm_time = new_alarm_time
            _LOGGER.info(
                "EZVIZ alarm received (serial=%s): code=%s name=%r time=%s",
                self.serial,
                merged.get("alarm_type_code"),
                merged.get("alarm_name"),
                new_alarm_time,
            )
        return merged
