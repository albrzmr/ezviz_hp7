"""EZVIZ HP7/CP7 API client."""
from __future__ import annotations

import logging
import time
from typing import Any

from .pylocalapi.client import EzvizClient
from .pylocalapi.camera import EzvizCamera
from .pylocalapi.cas import EzvizCAS

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

# Hardcoded CAS servers for each region (from pcap/libezstreamclient analysis)
CAS_SERVERS: dict[str, tuple[str, int]] = {
    "eu": ("54.72.248.29", 6500),
    "us": ("54.72.248.29", 6500),
    "cn": ("54.72.248.29", 6500),
}


class Hp7Api:
    """EZVIZ HP7/CP7 API client for cloud and local operations."""

    def __init__(
        self,
        username: str,
        password: str | None = None,
        region: str = "eu",
        token: dict[str, Any] | None = None,
    ) -> None:
        """Initialize EZVIZ API client.

        Args:
            username: EZVIZ account username.
            password: EZVIZ account password.
            region: API region (eu, us, cn, as, sa, ru).
            token: Optional cached authentication token.
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

    def ensure_client(self) -> None:
        """Ensure EzvizClient is initialized.

        Raises:
            RuntimeError: If client initialization fails.
        """
        if self._client:
            return

        try:
            self._client = EzvizClient(
                account=self._username,
                password=self._password,
                url=self._url,
                token=self._token,
            )

            if not self._token:
                self._login_and_store_token()
        except Exception as exc:
            _LOGGER.error("Failed to initialize EzvizClient: %s", exc)
            raise RuntimeError(f"Failed to initialize EZVIZ client: {exc}") from exc

    def _login_and_store_token(self) -> None:
        """Authenticate with EZVIZ server and store token.

        Raises:
            ValueError: If login fails.
        """
        if not self._client:
            raise RuntimeError("Client not initialized")

        try:
            self._token = self._client.login()
            _LOGGER.debug("EZVIZ authentication successful")
        except (ValueError, KeyError) as exc:
            _LOGGER.error("EZVIZ authentication failed: %s", exc)
            raise ValueError(f"Authentication failed: {exc}") from exc

    def login(self) -> bool:
        """Authenticate with EZVIZ server.

        Returns:
            True if authentication was successful.

        Raises:
            RuntimeError: If authentication fails.
        """
        self.ensure_client()
        return True

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
                _LOGGER.debug("Device %s detected as %s (sub_cat=%s)", serial, self.model, sub_cat)
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
                if (time.monotonic() - fetched_at) < self.AES_KEY_TTL:
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Token refresh failed before AES fetch: %s", exc)

        if not self._token:
            raise RuntimeError("no cloud token available")

        try:
            key = _try_once()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AES key fetch failed (will retry with re-login): %s", exc)
            # Force a re-login and retry once
            try:
                self._login_and_store_token()
            except Exception as relog_exc:  # noqa: BLE001
                raise RuntimeError(
                    f"AES fetch failed and re-login failed: {relog_exc}"
                ) from relog_exc
            key = _try_once()

        if not key or len(key) != 16:
            raise RuntimeError(f"invalid AES key from EUCAS: {key!r}")
        key_bytes = key.encode("ascii")
        self._aes_cache[bare] = (key_bytes, time.monotonic())
        return key_bytes

    def invalidate_aes_cache(self, serial: str | None = None) -> None:
        """Drop cached AES key(s).

        Call this when a stream session fails to decrypt — most likely
        cause is that the doorbell was re-paired, which rotates the
        key.  Next ``fetch_lan_aes_key`` call will hit EUCAS again.
        """
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
        except Exception as exc:  # noqa: BLE001
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
        except (KeyError, AttributeError, ValueError) as exc:
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
            except Exception as exc:
                _LOGGER.debug("Error during logout: %s", exc)
            finally:
                self._client = None

    def _try_unlock(self, serial: str, lock_no: int) -> bool:
        """Attempt to unlock a specific lock.

        Args:
            serial: Device serial number.
            lock_no: Lock number to unlock.

        Returns:
            True if unlock was successful.
        """
        self.ensure_client()
        if not self._token or not self._client:
            return False

        user_id = self._token.get("username") or self._username
        try:
            self._client.remote_unlock(serial, user_id, lock_no)
            _LOGGER.info("Remote unlock OK (serial=%s, lock_no=%s)", serial, lock_no)
            return True
        except (KeyError, AttributeError, ValueError, Exception) as exc:
            _LOGGER.warning(
                "Remote unlock failed (serial=%s, lock_no=%s): %s",
                serial,
                lock_no,
                exc,
            )
            return False

    def unlock_door(self, serial: str) -> bool:
        """Unlock the door lock."""
        return self._try_unlock(serial, DEFAULT_DOOR_LOCK_NO) or self._try_unlock(
            serial, DEFAULT_GATE_LOCK_NO
        )

    def unlock_gate(self, serial: str) -> bool:
        """Unlock the gate lock."""
        return self._try_unlock(serial, DEFAULT_GATE_LOCK_NO) or self._try_unlock(
            serial, DEFAULT_DOOR_LOCK_NO
        )

    def get_status(self, serial: str) -> dict[str, Any]:
        """Get current device status.

        Args:
            serial: Device serial number.

        Returns:
            Dictionary with device status and sensor readings.
        """
        self.ensure_client()
        if not self._client:
            return {}

        try:
            camera = EzvizCamera(self._client, serial)
            cam_status = camera.status(refresh=True)
            wifi_info = cam_status.get("WIFI", {})

            _LOGGER.debug("Device status received for %s", serial)

            return {
                "name": cam_status.get("name"),
                "version": cam_status.get("version"),
                "upgrade_available": cam_status.get("upgrade_available"),
                "status": cam_status.get("status"),
                "wan_ip": cam_status.get("wan_ip"),
                "pir_status": cam_status.get("PIR_Status"),
                "motion": cam_status.get("Motion_Trigger"),
                "seconds_last_trigger": cam_status.get("Seconds_Last_Trigger"),
                "last_alarm_time": cam_status.get("last_alarm_time"),
                "last_alarm_pic": cam_status.get("last_alarm_pic"),
                "alarm_name": cam_status.get("last_alarm_type_name"),
                "ssid": wifi_info.get("ssid"),
                "signal": wifi_info.get("signal"),
                "local_ip": cam_status.get("local_ip") or wifi_info.get("address"),
                "local_rtsp_port": cam_status.get("local_rtsp_port") or "554",
            }

        except (KeyError, AttributeError, ValueError, Exception) as exc:
            _LOGGER.warning("Failed to get device status for %s: %s", serial, exc)
            return {}
