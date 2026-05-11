"""EZVIZ HP7/CP7 API client."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import pyezvizapi.client as _ezviz_client_mod
from pyezvizapi.camera import EzvizCamera
from pyezvizapi.client import EzvizClient
from pyezvizapi.exceptions import (
    EzvizAuthTokenExpired,
    EzvizAuthVerificationCode,
    HTTPError,
    InvalidHost,
    InvalidURL,
    PyEzvizError,
)

# IMPORTANT: do NOT import ``EzvizCAS`` from ``pyezvizapi``.  The
# upstream version (1.0.4.9 and earlier) sends ``<ClientType>0</ClientType>``
# and reads the response with a single ``recv(1024)`` call without
# proper packet framing — which works for the legacy NetSDK login
# path but produces a garbled AES-128 control key when used against
# HP7 / CP7 firmware that expects ``ClientType=3`` and proper
# 32B-header / body / 32B-tail framing.  Garbled key → INVITE XML
# encrypted with the wrong secret → doorbell returns binary noise →
# ``xml.etree`` raises ``no element found: line 1, column 0`` and the
# LAN session aborts.
#
# We keep our own patched ``EzvizCAS`` here as a single vendored
# file until the fix is merged upstream.
from .pylocalapi.cas import EzvizCAS

if TYPE_CHECKING:
    from .stats import ActivityStats

_LOGGER = logging.getLogger(__name__)

DEFAULT_DOOR_LOCK_NO = 2
DEFAULT_GATE_LOCK_NO = 1

REGION_URLS: dict[str, str] = {
    "eu": "apiieu.ezvizlife.com",
    "us": "apiisa.ezvizlife.com",
    "cn": "apiicn.ezvizlife.com",
    "as": "apiias.ezvizlife.com",
    "sa": "apiisa.ezvizlife.com",
    "ru": "apirus.ezvizru.com",
}


class Hp7Api:
    """EZVIZ HP7/CP7 API client for cloud and local operations."""

    def __init__(
        self,
        username: str,
        password: str | None = None,
        region: str = "eu",
        token: dict[str, Any] | None = None,
        stats: ActivityStats | None = None,
        feature_code: str | None = None,
    ) -> None:
        """Initialize EZVIZ API client.

        Args:
            username: EZVIZ account username.
            password: EZVIZ account password.
            region: API region (eu, us, cn, as, sa, ru).
            token: Optional cached authentication token.
            stats: Activity counter — incremented on logins, AES fetches,
                cache hits/misses and CAS errors so the integration can
                emit a periodic usage summary.  Optional.
            feature_code: 32-char hex per-install featureCode.  When
                provided, monkey-patches ``pyezvizapi.client.FEATURE_CODE``
                and the session header so each install fingerprints
                differently — no global hardcoded value EZVIZ could
                blacklist.  Required for HP7 / CP7 LAN streaming
                because the same code is used to derive ``<Sign>`` in
                the EUCAS DirectConnect call.
        """
        self._username = username
        self._password = password
        self._region = region
        self._token = token
        self._client: EzvizClient | None = None
        self._url = REGION_URLS.get(region, REGION_URLS["eu"])
        self.supports_door = True
        self.supports_gate = True
        self.model: str = "HP7"
        self._stats = stats
        self._feature_code = feature_code
        # Cache for the AES-128 control key keyed by bare serial.  The
        # value is a ``(key_bytes, fetched_at_monotonic)`` tuple; entries
        # older than ``AES_KEY_TTL`` are refetched.  The cache is also
        # invalidated on any decrypt error from the LAN client (see
        # ``invalidate_aes_cache``).
        self._aes_cache: dict[str, tuple[bytes, float]] = {}

    # AES-128 control key cache TTL.  The key only changes when the
    # doorbell is re-paired (rare, manual user action), so a long TTL
    # is safe.  We refresh proactively from a background task in
    # ``__init__.py`` to keep cold-start latency near zero.
    AES_KEY_TTL: float = 30 * 60.0

    @property
    def token(self) -> dict[str, Any] | None:
        """Get the current authentication token."""
        return self._token

    def _apply_feature_code(self) -> None:
        """Patch ``pyezvizapi.client`` to use our per-install featureCode.

        ``pyezvizapi`` imports ``FEATURE_CODE`` from ``constants`` into
        the ``client`` module namespace and bakes it into login / device
        payloads and the default session header.  Both paths must be
        overridden for our random per-install code to take effect — and
        the patch must happen *before* ``EzvizClient(...)`` is
        constructed so the session header picks up the override at
        ``self._session.headers.update(REQUEST_HEADER)`` time.
        """
        if not self._feature_code:
            return
        _ezviz_client_mod.FEATURE_CODE = self._feature_code
        try:
            _ezviz_client_mod.REQUEST_HEADER["featureCode"] = self._feature_code
        except (AttributeError, TypeError):
            _LOGGER.debug("[EZVIZ-AUTH] could not patch REQUEST_HEADER")

    def ensure_client(self) -> None:
        """Ensure EzvizClient is initialized.

        Raises:
            RuntimeError: If client initialization fails.
        """
        if self._client:
            return

        try:
            self._apply_feature_code()
            self._client = EzvizClient(
                account=self._username,
                password=self._password,
                url=self._url,
                token=self._token,
            )

            if not self._token:
                self._login_and_store_token()
        except (
            PyEzvizError,
            HTTPError,
            InvalidHost,
            InvalidURL,
            ValueError,
            KeyError,
            OSError,
        ) as exc:
            _LOGGER.error("Failed to initialize EzvizClient: %s", exc)
            raise RuntimeError(f"Failed to initialize EZVIZ client: {exc}") from exc

    def _login_and_store_token(self) -> None:
        """Authenticate with EZVIZ server and store token.

        Raises:
            ValueError: If login fails.
        """
        if not self._client:
            raise RuntimeError("Client not initialized")

        t0 = time.monotonic()
        try:
            self._token = self._client.login()
            elapsed = time.monotonic() - t0
            _LOGGER.info(
                "[EZVIZ-AUTH] cloud login OK (%.0f ms, account=%s, region=%s)",
                elapsed * 1000,
                self._username,
                self._region,
            )
            if self._stats is not None:
                self._stats.cloud_logins += 1
        except (ValueError, KeyError) as exc:
            _LOGGER.error("[EZVIZ-AUTH] cloud login FAILED: %s", exc)
            raise ValueError(f"Authentication failed: {exc}") from exc

    def login(self) -> None:
        """Authenticate with EZVIZ server.

        Raises:
            RuntimeError: If authentication fails.
        """
        self.ensure_client()

    def detect_capabilities(self, serial: str) -> None:
        """Detect device capabilities from EZVIZ API.

        Args:
            serial: Device serial number (may include sub-device suffix).
        """
        self.ensure_client()
        try:
            if not self._client:
                return

            # Try sub-serial first; if empty, fall back to main serial
            main_serial = serial.split("-")[0] if "-" in serial else serial
            dev = self._client.get_device_infos(main_serial)

            sub_cat = (
                dev.get("deviceSubCategory")
                or dev.get("deviceInfos", {}).get("deviceSubCategory")
                or ""
            ).upper()

            # Also check resourceInfos for each sub-device
            if "CP7" not in sub_cat:
                for res in dev.get("resourceInfos") or []:
                    rsc = (res.get("deviceSubCategory") or "").upper()
                    if "CP7" in rsc:
                        sub_cat = rsc
                        break

            if "CP7" in sub_cat:
                self.model = "CP7"
                _LOGGER.debug("Device %s detected as CP7", serial)
            else:
                _LOGGER.debug(
                    "Device %s detected as %s (sub_cat=%s)", serial, self.model, sub_cat
                )
        except (KeyError, AttributeError, ValueError) as exc:
            _LOGGER.debug("Failed to detect capabilities for %s: %s", serial, exc)

        self.supports_door = True
        self.supports_gate = True

    # ── LAN AES key + related-device helpers (used by tcp_relay) ──────

    def fetch_lan_aes_key(self, serial: str, force: bool = False) -> bytes:
        """Return the 16-byte AES-128 control key for the given device.

        Returns a cached value if one is available and younger than
        ``AES_KEY_TTL``.  When the cache misses (or ``force=True``):
        refreshes the cloud token so the EUCAS call carries a valid
        ClientID, queries EUCAS cmd 0x2001 ``DirectConnect`` and
        retries once with a fresh login on failure to handle JWT
        expiry transparently.

        Raises:
            RuntimeError: if the key cannot be obtained.
        """
        bare = serial.split("-")[0] if "-" in serial else serial

        if not force:
            cached = self._aes_cache.get(bare)
            if cached is not None:
                key, fetched_at = cached
                age = time.monotonic() - fetched_at
                if age < self.AES_KEY_TTL:
                    if self._stats is not None:
                        self._stats.aes_cache_hits += 1
                    _LOGGER.debug(
                        "[EZVIZ-AES] cache HIT for %s (age=%.0fs)",
                        bare,
                        age,
                    )
                    return key

        def _try_once() -> str:
            cas = EzvizCAS(self._token)
            info = cas.cas_get_encryption(bare)
            session = info.get("Response", {}).get("Session", {})
            return str(session.get("@Key") or "")

        self.ensure_client()
        # Refresh JWT so service_urls is current
        if self._client:
            try:
                tok = self._client.login()
                if tok:
                    self._token = tok
            except (
                PyEzvizError,
                HTTPError,
                InvalidHost,
                InvalidURL,
                EzvizAuthTokenExpired,
                EzvizAuthVerificationCode,
                ValueError,
                KeyError,
                OSError,
            ) as exc:
                _LOGGER.debug(
                    "[EZVIZ-AES] token refresh failed before AES fetch: %s",
                    exc,
                )

        if not self._token:
            raise RuntimeError("no cloud token available")

        if force:
            if self._stats is not None:
                self._stats.aes_force_refreshes += 1
            _LOGGER.info("[EZVIZ-AES] cache forced-refresh for %s", bare)
        else:
            if self._stats is not None:
                self._stats.aes_cache_misses += 1
            _LOGGER.info("[EZVIZ-AES] cache MISS for %s — calling EUCAS", bare)

        t0 = time.monotonic()
        try:
            key_str = _try_once()
        except Exception as exc:
            if self._stats is not None:
                self._stats.errors_cas += 1
            _LOGGER.warning(
                "[EZVIZ-AES] EUCAS call FAILED (%s) — retrying with fresh login",
                exc,
            )
            # Force a re-login and retry once
            try:
                self._login_and_store_token()
                if self._stats is not None:
                    self._stats.cloud_relogins += 1
            except Exception as relog_exc:
                if self._stats is not None:
                    self._stats.errors_cas += 1
                raise RuntimeError(
                    f"AES fetch failed and re-login failed: {relog_exc}"
                ) from relog_exc
            key_str = _try_once()

        elapsed = time.monotonic() - t0
        if not key_str or len(key_str) != 16:
            raise RuntimeError(f"invalid AES key from EUCAS: {key_str!r}")
        key_bytes = key_str.encode("ascii")
        # Detect a key rotation (re-pairing) — useful signal for the user.
        prior = self._aes_cache.get(bare)
        if prior is not None and prior[0] != key_bytes:
            _LOGGER.warning(
                "[EZVIZ-AES] KEY ROTATED for %s — doorbell appears to have been "
                "re-paired since last fetch",
                bare,
            )
        self._aes_cache[bare] = (key_bytes, time.monotonic())
        _LOGGER.info(
            "[EZVIZ-AES] EUCAS fetch OK for %s (%.0f ms)",
            bare,
            elapsed * 1000,
        )
        return key_bytes

    def invalidate_aes_cache(self, serial: str | None = None) -> None:
        """Drop cached AES key(s).

        Call this when a stream session fails to decrypt — most likely
        cause is that the doorbell was re-paired, which rotates the
        key.  Next ``fetch_lan_aes_key`` call will hit EUCAS again.
        """
        if self._stats is not None:
            self._stats.aes_invalidations += 1
        _LOGGER.info("[EZVIZ-AES] cache invalidated (serial=%s)", serial or "all")
        if serial is None:
            self._aes_cache.clear()
            return
        bare = serial.split("-")[0] if "-" in serial else serial
        self._aes_cache.pop(bare, None)

    def get_related_device(self, serial: str) -> str:
        """Resolve the camera-module sub-serial used in <Channel RelatedDevice>.

        For HP7 a hyphenated config like ``MAINSERIAL-CAMSERIAL`` already carries it.
        Otherwise we ask the cloud (``get_device_infos``) and pick the first
        sub-device whose serial differs from the main one.  Falls back to
        the main serial if nothing better is available — the doorbell
        sometimes accepts that.
        """
        if "-" in serial:
            return serial.split("-", 1)[1]

        self.ensure_client()
        if not self._client:
            return serial
        try:
            dev = self._client.get_device_infos(serial)
        except (
            PyEzvizError,
            HTTPError,
            InvalidHost,
            InvalidURL,
            ValueError,
            KeyError,
            OSError,
        ) as exc:
            _LOGGER.debug("get_device_infos failed for %s: %s", serial, exc)
            return serial

        candidates: list[str] = []
        for res in dev.get("resourceInfos") or []:
            sub = (res.get("deviceSerial") or "").strip()
            if sub and sub != serial:
                candidates.append(sub)
        for sub in dev.get("cameraInfos") or []:
            s = (sub.get("deviceSerial") or "").strip() if isinstance(sub, dict) else ""
            if s and s != serial and s not in candidates:
                candidates.append(s)

        if candidates:
            _LOGGER.debug("related-device for %s = %s", serial, candidates[0])
            return candidates[0]
        return serial

    def list_devices(self) -> dict[str, dict[str, Any]]:
        """List all paired EZVIZ devices.

        Returns:
            Dictionary mapping device serial to device info.
        """
        self.ensure_client()
        if not self._client:
            return {}

        try:
            devices = self._client.get_device_infos()
        except (
            PyEzvizError,
            HTTPError,
            InvalidHost,
            InvalidURL,
            KeyError,
            AttributeError,
            ValueError,
            OSError,
        ) as exc:
            _LOGGER.warning("Failed to list devices: %s", exc)
            return {}

        result: dict[str, dict[str, Any]] = {}
        for serial, data in devices.items():
            name = data.get("name") or data.get("deviceName") or "Device"
            result[serial] = {"device_name": name}
        return result

    def close(self) -> None:
        """Close API connection and cleanup resources."""
        if self._client:
            try:
                self._client.logout()
            except (PyEzvizError, HTTPError, OSError) as exc:
                _LOGGER.debug("Error during logout: %s", exc)
            finally:
                self._client = None

    def _try_unlock(self, serial: str, lock_no: int) -> bool:
        """Attempt to unlock a specific lock.

        Args:
            serial: Device serial number.
            lock_no: Lock number to unlock.

        Returns:
            True if unlock was successful, False otherwise (network
            error, invalid lock number, account / device mismatch …).
        """
        self.ensure_client()
        if not self._token or not self._client:
            return False

        user_id = self._token.get("username") or self._username
        try:
            self._client.remote_unlock(serial, user_id, lock_no)
        except Exception as exc:
            _LOGGER.warning(
                "Remote unlock failed (serial=%s, lock_no=%s): %s",
                serial,
                lock_no,
                exc,
            )
            return False
        _LOGGER.info("Remote unlock OK (serial=%s, lock_no=%s)", serial, lock_no)
        return True

    def unlock_door(self, serial: str) -> bool:
        """Unlock the door lock (lock #2 by default).

        No fallback to the gate lock if this call fails — pressing
        "unlock door" should never open the gate.  Same for
        ``unlock_gate``.
        """
        return self._try_unlock(serial, DEFAULT_DOOR_LOCK_NO)

    def unlock_gate(self, serial: str) -> bool:
        """Unlock the gate lock (lock #1 by default).

        Mirror of ``unlock_door`` — no cross-fallback.
        """
        return self._try_unlock(serial, DEFAULT_GATE_LOCK_NO)

    def get_status(self, serial: str) -> dict[str, Any]:
        """Get current device status.

        Propagates any error from the underlying cloud call so that the
        coordinator can wrap it in ``UpdateFailed`` and HA marks
        entities ``unavailable`` correctly.  Returning ``{}`` on errors
        (the previous behaviour) hides connectivity failures from the
        user and keeps stale entity values around forever.
        """
        self.ensure_client()
        if not self._client:
            raise RuntimeError("EZVIZ HP7 cloud client not initialised")

        camera = EzvizCamera(self._client, serial)
        cam_status = camera.status(refresh=True)
        wifi_info = cam_status.get("WIFI") or {}
        _LOGGER.debug("Device status received for %s", serial)

        return {
            "name": cam_status.get("name"),
            "version": cam_status.get("version"),
            "upgrade_available": cam_status.get("upgrade_available"),
            "status": cam_status.get("status"),
            "wan_ip": cam_status.get("wan_ip"),
            # ``PIR_Status`` and ``Motion_Trigger`` aren't populated by
            # the HP7 / CP7 firmware (verified across hundreds of
            # polls) — no entity reads them, so we don't pretend to
            # expose them.  Motion / presence detection is signalled
            # via the cloud alarm timeline (``last_alarm_time`` plus
            # ``alarm_name``) which the binary sensors use.
            "seconds_last_trigger": cam_status.get("Seconds_Last_Trigger"),
            "last_alarm_time": cam_status.get("last_alarm_time"),
            "last_alarm_pic": cam_status.get("last_alarm_pic"),
            "alarm_name": cam_status.get("last_alarm_type_name"),
            "ssid": wifi_info.get("ssid"),
            "signal": wifi_info.get("signal"),
            "local_ip": cam_status.get("local_ip") or wifi_info.get("address"),
            "local_rtsp_port": cam_status.get("local_rtsp_port") or "554",
        }
