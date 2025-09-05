from __future__ import annotations
import json
import logging
import shutil
import subprocess
from typing import Any, Dict, Optional

from pyezvizapi.client import EzvizClient

_LOGGER = logging.getLogger(__name__)

DEFAULT_DOOR_LOCK_NO = 2   # PORTA=2
DEFAULT_GATE_LOCK_NO = 1   # CANCELLO=1


class Hp7Api:


    def __init__(self, username: str, password: str, region: str):
        self._username = username
        self._password = password
        self._region_or_url = region
        self._client: Optional[EzvizClient] = None
        self._user_id: Optional[str] = None
        self._cli = shutil.which("pyezvizapi")

        self.supports_door = True
        self.supports_gate = True

    # -------------------- Sessione SDK (solo per unlock) --------------------

    def ensure_client(self) -> None:
        if self._client is not None:
            return
        self._client = EzvizClient(
            account=self._username,
            password=self._password,
            url=self._region_or_url,
        )
        self._client.login()
        _LOGGER.info("EZVIZ HP7: login OK su %s", self._client._token.get("api_url"))

    def login(self) -> bool:
        """Compat per il setup: inizializza il client SDK."""
        self.ensure_client()
        return True

    def detect_capabilities(self, serial: str) -> None:
        """Compat per il setup: per ora li consideriamo supportati."""
        try:
            self.ensure_client()
            dev = self._client.get_device_infos(serial)
            cat = dev.get("deviceInfos", {}).get("deviceCategory")
            sub = dev.get("deviceInfos", {}).get("deviceSubCategory")
            _LOGGER.info("EZVIZ HP7: device %s category=%s sub=%s", serial, cat, sub)
        except Exception as e:
            _LOGGER.debug("detect_capabilities get_device_infos fallita: %s", e)
        self.supports_door = True
        self.supports_gate = True

    def _ensure_user_id(self) -> str:
        if self._user_id:
            return self._user_id
        self.ensure_client()
        info = self._client.get_user_id()
        for k in ("userId", "username", "userName", "uid"):
            if isinstance(info, dict) and info.get(k):
                self._user_id = str(info[k])
                break
        if not self._user_id:
            self._user_id = self._username
            _LOGGER.warning("user_id non trovato; uso username come fallback.")
        return self._user_id

    # -------------------- CLI helper (status/sensori) --------------------

    def _run_cli(self, args: list[str]) -> tuple[bool, str]:
        if not self._cli:
            _LOGGER.error("CLI 'pyezvizapi' non trovata nel PATH del container.")
            return False, ""
        cmd = [self._cli, "-u", self._username, "-p", self._password, "-r", self._region_or_url] + args
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=25)
            return True, out.decode("utf-8", "ignore")
        except subprocess.CalledProcessError as e:
            _LOGGER.error("CLI error (%s): %s", e.returncode, (e.output or b"").decode("utf-8", "ignore"))
            return False, ""
        except Exception as e:
            _LOGGER.error("CLI exception: %s", e)
            return False, ""

    # -------------------- Discovery & Status --------------------

    def list_devices(self) -> Dict[str, Dict[str, Any]]:
        self.ensure_client()
        cams = self._client.load_cameras()  # dict per serial
        result: Dict[str, Dict[str, Any]] = {}
        for serial, data in (cams or {}).items():
            name = (
                data.get("STATUS", {}).get("name")
                or data.get("deviceInfos", {}).get("deviceName")
                or "Device"
            )
            result[serial] = {"device_name": name}
        return result

    def get_status(self, serial: str) -> Dict[str, Any]:
        ok, out = self._run_cli(["camera", "--serial", serial, "status"])
        if not ok:
            return {}
        try:
            data = json.loads(out)
            if not isinstance(data, dict):
                data = {}
        except Exception as e:
            _LOGGER.error("Parse JSON status fallito: %s", e)
            data = {}

        if data:
            # Prova a recuperare il codice di verifica per lo streaming RTSP.
            try:
                self.ensure_client()
                auth = self._client.get_cam_auth_code(serial)
                if isinstance(auth, dict) and auth.get("devAuthCode"):
                    data["rtsp_password"] = auth["devAuthCode"]
            except Exception as e:  # noqa: BLE001 - meglio non interrompere lo status
                _LOGGER.debug("get_cam_auth_code fallita: %s", e)

        return data

    # -------------------- Sblocco --------------------

    def _try_unlock(self, serial: str, lock_no: int) -> bool:
        self.ensure_client()
        user_id = self._ensure_user_id()
        try:
            self._client.remote_unlock(serial, user_id, lock_no)
            _LOGGER.info("remote_unlock OK (serial=%s, lock_no=%s)", serial, lock_no)
            return True
        except Exception as e:
            _LOGGER.warning("remote_unlock KO (serial=%s, lock_no=%s): %s", serial, lock_no, e)
            return False

    def unlock_door(self, serial: str) -> bool:
        return self._try_unlock(serial, DEFAULT_DOOR_LOCK_NO) or \
               self._try_unlock(serial, DEFAULT_GATE_LOCK_NO)

    def unlock_gate(self, serial: str) -> bool:
        return self._try_unlock(serial, DEFAULT_GATE_LOCK_NO) or \
               self._try_unlock(serial, DEFAULT_DOOR_LOCK_NO)
