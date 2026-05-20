"""Tests for ``custom_components.ezviz_hp7.api.Hp7Api``.

Phase 1.1 of the testing plan — pure-Python coverage of every code
path in ``api.py``.  Mocks the boundary (``EzvizClient`` /
``EzvizCamera`` / ``EzvizCAS``) and lets the rest of ``Hp7Api`` run
unmodified.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import MagicMock

import pytest
from pyezvizapi.exceptions import PyEzvizError

from custom_components.ezviz_hp7 import api as api_mod
from custom_components.ezviz_hp7.api import (
    DEFAULT_ALARM_PIC_URL,
    DEFAULT_DOOR_LOCK_NO,
    DEFAULT_GATE_LOCK_NO,
    REGION_URLS,
    Hp7Api,
    Hp7EzvizCamera,
)
from custom_components.ezviz_hp7.stats import ActivityStats

VALID_AES_KEY = "0123456789ABCDEF"  # 16-char ascii
ROTATED_AES_KEY = "FEDCBA9876543210"


# ── __init__ ────────────────────────────────────────────────────────


def test_init_stores_region_url() -> None:
    api = Hp7Api(username="u", password="p", region="eu")
    assert api._url == REGION_URLS["eu"]
    assert api._username == "u"
    assert api._password == "p"
    assert api._token is None
    assert api._client is None


def test_init_unknown_region_falls_back_to_eu() -> None:
    api = Hp7Api(username="u", region="atlantis")
    assert api._url == REGION_URLS["eu"]


def test_init_defaults() -> None:
    api = Hp7Api(username="u")
    assert api.model == "HP7"
    assert api.supports_door is True
    assert api.supports_gate is True
    assert api._aes_cache == {}
    assert api._stats is None
    assert api._feature_code is None


def test_aes_key_ttl_is_30_minutes() -> None:
    assert Hp7Api.AES_KEY_TTL == 30 * 60.0


def test_region_table_has_all_documented_regions() -> None:
    assert set(REGION_URLS) >= {"eu", "us", "cn", "as", "sa", "ru"}


# ── _apply_feature_code ─────────────────────────────────────────────


def test_apply_feature_code_noop_when_none(monkeypatch) -> None:
    """``feature_code=None`` must NOT touch the global FEATURE_CODE."""
    sentinel = "ORIGINAL-DO-NOT-TOUCH"
    monkeypatch.setattr(api_mod._ezviz_client_mod, "FEATURE_CODE", sentinel)
    api = Hp7Api(username="u", feature_code=None)
    api._apply_feature_code()
    assert sentinel == api_mod._ezviz_client_mod.FEATURE_CODE


def test_apply_feature_code_patches_module_and_header(monkeypatch) -> None:
    monkeypatch.setattr(api_mod._ezviz_client_mod, "FEATURE_CODE", "OLD")
    monkeypatch.setitem(api_mod._ezviz_client_mod.REQUEST_HEADER, "featureCode", "OLD")
    api = Hp7Api(username="u", feature_code="NEW")
    api._apply_feature_code()
    assert api_mod._ezviz_client_mod.FEATURE_CODE == "NEW"
    assert api_mod._ezviz_client_mod.REQUEST_HEADER["featureCode"] == "NEW"


def test_apply_feature_code_swallows_header_failure(monkeypatch) -> None:
    """A weird REQUEST_HEADER must not crash setup — debug-log only."""
    monkeypatch.setattr(api_mod._ezviz_client_mod, "FEATURE_CODE", "OLD")

    # Replace REQUEST_HEADER with an object whose __setitem__ raises.
    class _Broken:
        def __setitem__(self, _key, _value):
            raise TypeError("not subscriptable")

    monkeypatch.setattr(api_mod._ezviz_client_mod, "REQUEST_HEADER", _Broken())
    api = Hp7Api(username="u", feature_code="NEW")
    # Should NOT raise.
    api._apply_feature_code()
    assert api_mod._ezviz_client_mod.FEATURE_CODE == "NEW"


# ── ensure_client ───────────────────────────────────────────────────


def test_ensure_client_idempotent(patched_api) -> None:
    """Second call must not re-construct ``EzvizClient``."""
    api_mod.EzvizClient.reset_mock()
    patched_api.ensure_client()
    api_mod.EzvizClient.assert_not_called()


def test_ensure_client_logs_in_when_no_token(mocker) -> None:
    """No token → login() called → token stored on the instance."""
    fake_token = {"session_id": "tok", "username": "u"}
    fake_client_cls = mocker.patch("custom_components.ezviz_hp7.api.EzvizClient")
    fake_client_cls.return_value.login.return_value = fake_token
    api = Hp7Api(username="u", password="p", region="eu", token=None)
    api.ensure_client()
    assert api._token == fake_token
    fake_client_cls.return_value.login.assert_called_once()


def test_ensure_client_wraps_pyezviz_error(mocker) -> None:
    mocker.patch(
        "custom_components.ezviz_hp7.api.EzvizClient",
        side_effect=PyEzvizError("boom"),
    )
    api = Hp7Api(username="u", password="p", token=None)
    with pytest.raises(RuntimeError, match="Failed to initialize EZVIZ client"):
        api.ensure_client()


# ── _login_and_store_token ──────────────────────────────────────────


def test_login_and_store_requires_client() -> None:
    api = Hp7Api(username="u")
    with pytest.raises(RuntimeError, match="Client not initialized"):
        api._login_and_store_token()


def test_login_and_store_bumps_cloud_logins(patched_api) -> None:
    stats = ActivityStats()
    patched_api._stats = stats
    patched_api._login_and_store_token()
    assert stats.cloud_logins == 1


def test_login_and_store_raises_value_error_on_auth_failure(patched_api) -> None:
    patched_api._client.login.side_effect = ValueError("bad creds")
    with pytest.raises(ValueError, match="Authentication failed"):
        patched_api._login_and_store_token()


# ── login() smoke ──────────────────────────────────────────────────


def test_login_succeeds_without_returning_value(patched_api) -> None:
    # ``login()`` is a thin wrapper around ``ensure_client``; smoke check only.
    assert patched_api.login() is None


# ── detect_capabilities ────────────────────────────────────────────


def test_detect_capabilities_flips_to_cp7_when_top_level(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {"deviceSubCategory": "CP7"}
    patched_api.detect_capabilities("BE0000000")
    assert patched_api.model == "CP7"
    # Always splits hyphenated input to call the cloud with the bare serial.
    patched_api._client.get_device_infos.assert_called_with("BE0000000")


def test_detect_capabilities_uses_main_serial_when_hyphenated(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {"deviceSubCategory": "HP7"}
    patched_api.detect_capabilities("MAIN1234-SUB5678")
    patched_api._client.get_device_infos.assert_called_with("MAIN1234")
    assert patched_api.model == "HP7"


def test_detect_capabilities_picks_up_cp7_from_resource_infos(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {
        "deviceSubCategory": "OTHER",
        "resourceInfos": [
            {"deviceSubCategory": "FOO"},
            {"deviceSubCategory": "cp7"},  # lowercase — must still match
        ],
    }
    patched_api.detect_capabilities("X")
    assert patched_api.model == "CP7"


def test_detect_capabilities_stays_hp7_when_nothing_matches(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {"deviceSubCategory": "BLAH"}
    patched_api.detect_capabilities("X")
    assert patched_api.model == "HP7"


def test_detect_capabilities_swallows_exceptions(patched_api) -> None:
    patched_api._client.get_device_infos.side_effect = KeyError("nope")
    # No raise — and supports flags stay True after the recovery path.
    patched_api.detect_capabilities("X")
    assert patched_api.supports_door is True
    assert patched_api.supports_gate is True


# ── fetch_lan_aes_key ───────────────────────────────────────────────


def _set_aes_response(key: str) -> None:
    api_mod.EzvizCAS.return_value.cas_get_encryption.return_value = {
        "Response": {"Session": {"@Key": key}}
    }


def test_fetch_lan_aes_cache_hit(patched_api) -> None:
    stats = ActivityStats()
    patched_api._stats = stats
    patched_api._aes_cache["TEST00001"] = (b"X" * 16, time.monotonic())
    out = patched_api.fetch_lan_aes_key("TEST00001-TEST00001")
    assert out == b"X" * 16
    assert stats.aes_cache_hits == 1
    api_mod.EzvizCAS.assert_not_called()


def test_fetch_lan_aes_cache_expired(patched_api) -> None:
    """Cached value older than TTL → re-fetch."""
    stats = ActivityStats()
    patched_api._stats = stats
    expired = time.monotonic() - (Hp7Api.AES_KEY_TTL + 1)
    patched_api._aes_cache["BARE"] = (b"OLD-KEY-IGNORED!", expired)
    _set_aes_response(VALID_AES_KEY)
    out = patched_api.fetch_lan_aes_key("BARE")
    assert out == VALID_AES_KEY.encode("ascii")
    assert stats.aes_cache_misses == 1
    assert stats.aes_cache_hits == 0


def test_fetch_lan_aes_cache_miss_calls_eucas(patched_api) -> None:
    stats = ActivityStats()
    patched_api._stats = stats
    _set_aes_response(VALID_AES_KEY)
    out = patched_api.fetch_lan_aes_key("TEST00001")
    assert out == VALID_AES_KEY.encode("ascii")
    assert stats.aes_cache_misses == 1
    api_mod.EzvizCAS.return_value.cas_get_encryption.assert_called_with("TEST00001")
    # Cache populated with the fetched key.
    assert patched_api._aes_cache["TEST00001"][0] == VALID_AES_KEY.encode("ascii")


def test_fetch_lan_aes_force_bypasses_cache(patched_api) -> None:
    stats = ActivityStats()
    patched_api._stats = stats
    patched_api._aes_cache["TEST00001"] = (b"CACHED-IGNORED!!", time.monotonic())
    _set_aes_response(VALID_AES_KEY)
    out = patched_api.fetch_lan_aes_key("TEST00001-suf", force=True)
    assert out == VALID_AES_KEY.encode("ascii")
    assert stats.aes_force_refreshes == 1
    assert stats.aes_cache_hits == 0


def test_fetch_lan_aes_retry_after_eucas_failure(patched_api) -> None:
    """First EUCAS call raises → re-login → second call succeeds."""
    stats = ActivityStats()
    patched_api._stats = stats
    api_mod.EzvizCAS.return_value.cas_get_encryption.side_effect = [
        RuntimeError("transient"),
        {"Response": {"Session": {"@Key": VALID_AES_KEY}}},
    ]
    out = patched_api.fetch_lan_aes_key("BARE")
    assert out == VALID_AES_KEY.encode("ascii")
    assert stats.errors_cas == 1
    # ``_login_and_store_token`` should have been invoked once on retry,
    # which itself bumps ``cloud_logins``.
    assert stats.cloud_relogins == 1
    assert stats.cloud_logins == 1


def test_fetch_lan_aes_retry_relogin_failure(patched_api) -> None:
    """EUCAS fails AND re-login fails → bubble up as RuntimeError."""
    stats = ActivityStats()
    patched_api._stats = stats
    api_mod.EzvizCAS.return_value.cas_get_encryption.side_effect = RuntimeError("e1")
    patched_api._client.login.side_effect = ValueError("relogin-fail")
    with pytest.raises(RuntimeError, match="AES fetch failed and re-login failed"):
        patched_api.fetch_lan_aes_key("BARE")
    # One CAS error from the first call, plus one from the re-login fail.
    assert stats.errors_cas == 2


def test_fetch_lan_aes_key_rotation_logs_warning(patched_api, caplog) -> None:
    prior = b"AAAAAAAAAAAAAAAA"
    patched_api._aes_cache["BARE"] = (prior, time.monotonic() - 10000)
    _set_aes_response(ROTATED_AES_KEY)
    with caplog.at_level(logging.WARNING, logger="custom_components.ezviz_hp7.api"):
        patched_api.fetch_lan_aes_key("BARE")
    assert any("KEY ROTATED" in m for m in caplog.messages)


def test_fetch_lan_aes_invalid_length_raises(patched_api) -> None:
    _set_aes_response("too-short")
    with pytest.raises(RuntimeError, match="invalid AES key"):
        patched_api.fetch_lan_aes_key("BARE")


def test_fetch_lan_aes_requires_token(patched_api) -> None:
    """No token after the pre-call refresh → bail out."""
    patched_api._token = None
    patched_api._client.login.return_value = None  # refresh stays None
    with pytest.raises(RuntimeError, match="no cloud token"):
        patched_api.fetch_lan_aes_key("BARE")


def test_fetch_lan_aes_refresh_token_swallows_errors(patched_api) -> None:
    """Pre-call ``client.login`` may fail — swallow + carry on."""
    stats = ActivityStats()
    patched_api._stats = stats
    patched_api._client.login.side_effect = PyEzvizError("transient refresh fail")
    _set_aes_response(VALID_AES_KEY)
    out = patched_api.fetch_lan_aes_key("BARE")
    assert out == VALID_AES_KEY.encode("ascii")


# ── invalidate_aes_cache ───────────────────────────────────────────


def test_invalidate_aes_cache_all() -> None:
    api = Hp7Api(username="u", stats=ActivityStats())
    api._aes_cache = {"A": (b"x", 0.0), "B": (b"y", 0.0)}
    api.invalidate_aes_cache(None)
    assert api._aes_cache == {}
    assert api._stats.aes_invalidations == 1


def test_invalidate_aes_cache_single() -> None:
    api = Hp7Api(username="u")
    api._aes_cache = {"A": (b"x", 0.0), "B": (b"y", 0.0)}
    api.invalidate_aes_cache("A-suffix")
    assert "A" not in api._aes_cache
    assert "B" in api._aes_cache


# ── get_related_device ─────────────────────────────────────────────


def test_get_related_device_hyphenated(patched_api) -> None:
    """Hyphenated serial → return the suffix without a cloud call."""
    out = patched_api.get_related_device("MAIN1234-SUB5678")
    assert out == "SUB5678"
    patched_api._client.get_device_infos.assert_not_called()


def test_get_related_device_from_resource_infos(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {
        "resourceInfos": [
            {"deviceSerial": "MAIN"},  # same as main → ignored
            {"deviceSerial": "SUB-1"},
        ],
    }
    assert patched_api.get_related_device("MAIN") == "SUB-1"


def test_get_related_device_falls_back_to_camera_infos(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {
        "resourceInfos": [],
        "cameraInfos": [{"deviceSerial": "CAM-9"}],
    }
    assert patched_api.get_related_device("MAIN") == "CAM-9"


def test_get_related_device_no_candidates_returns_main(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {"resourceInfos": []}
    assert patched_api.get_related_device("MAIN") == "MAIN"


def test_get_related_device_swallows_exception(patched_api) -> None:
    patched_api._client.get_device_infos.side_effect = PyEzvizError("nope")
    assert patched_api.get_related_device("MAIN") == "MAIN"


# ── list_devices ───────────────────────────────────────────────────


def test_list_devices_normalises_names(patched_api) -> None:
    patched_api._client.get_device_infos.return_value = {
        "S1": {"name": "Front Door"},
        "S2": {"deviceName": "Back Door"},
        "S3": {},  # no name → fall back to "Device"
    }
    out = patched_api.list_devices()
    assert out == {
        "S1": {"device_name": "Front Door"},
        "S2": {"device_name": "Back Door"},
        "S3": {"device_name": "Device"},
    }


def test_list_devices_returns_empty_on_error(patched_api) -> None:
    patched_api._client.get_device_infos.side_effect = PyEzvizError("nope")
    assert patched_api.list_devices() == {}


# ── _try_unlock / unlock_door / unlock_gate ─────────────────────────


def test_try_unlock_happy_path(patched_api) -> None:
    assert patched_api._try_unlock("SER", 2) is True
    patched_api._client.remote_unlock.assert_called_once_with(
        "SER", "user@example.com", 2
    )


def test_try_unlock_returns_false_on_exception(patched_api) -> None:
    patched_api._client.remote_unlock.side_effect = RuntimeError("nope")
    assert patched_api._try_unlock("SER", 2) is False


def test_try_unlock_uses_username_when_token_missing_username(patched_api) -> None:
    patched_api._token = {"session_id": "tok"}  # no ``username`` field
    patched_api._try_unlock("SER", 1)
    patched_api._client.remote_unlock.assert_called_once_with(
        "SER", "user@example.com", 1
    )


def test_unlock_door_uses_lock_no_2(patched_api) -> None:
    patched_api.unlock_door("SER")
    patched_api._client.remote_unlock.assert_called_once_with(
        "SER", "user@example.com", DEFAULT_DOOR_LOCK_NO
    )


def test_unlock_gate_uses_lock_no_1(patched_api) -> None:
    patched_api.unlock_gate("SER")
    patched_api._client.remote_unlock.assert_called_once_with(
        "SER", "user@example.com", DEFAULT_GATE_LOCK_NO
    )


# ── get_static_status ─────────────────────────────────────────────


def _set_static_status_payload(payload: dict) -> None:
    """Helper: ``Hp7EzvizCamera.status_static_dict()`` returns ``payload``."""
    api_mod.Hp7EzvizCamera.return_value.status_static_dict.return_value = payload


def test_get_static_status_raises_when_client_uninitialised(monkeypatch) -> None:
    api = Hp7Api(username="u")
    monkeypatch.setattr(api, "ensure_client", lambda: None)
    with pytest.raises(RuntimeError, match="cloud client not initialised"):
        api.get_static_status("SER")


def test_get_static_status_calls_pagelist_only(patched_api) -> None:
    """Static poll hits ``get_device_infos`` (pagelist) but **not**
    ``get_device_messages_list`` (alarms) — that's the whole point."""
    _set_static_status_payload({"name": "Doorbell", "WIFI": {}})
    patched_api.get_static_status("SER")
    patched_api._client.get_device_infos.assert_called_once_with("SER")
    patched_api._client.get_device_messages_list.assert_not_called()


def test_get_static_status_maps_device_and_wifi_fields(patched_api) -> None:
    _set_static_status_payload(
        {
            "name": "Doorbell",
            "version": "V5.3.6 build 250825",
            "upgrade_available": False,
            "status": 1,
            "wan_ip": "203.0.113.1",
            "WIFI": {"ssid": "Casa", "signal": 80, "address": "WIFI-IP"},
            "local_ip": "192.0.2.10",
            "local_rtsp_port": "8554",
        }
    )
    out = patched_api.get_static_status("SER")
    assert out == {
        "name": "Doorbell",
        "version": "V5.3.6 build 250825",
        "upgrade_available": False,
        "status": 1,
        "wan_ip": "203.0.113.1",
        "ssid": "Casa",
        "signal": 80,
        "local_ip": "192.0.2.10",
        "local_rtsp_port": "8554",
        "image_encryption": False,
    }


def test_get_static_status_falls_back_local_ip_to_wifi_address(patched_api) -> None:
    _set_static_status_payload(
        {"name": "D", "WIFI": {"address": "10.0.0.5"}, "local_ip": None}
    )
    out = patched_api.get_static_status("SER")
    assert out["local_ip"] == "10.0.0.5"


def test_get_static_status_local_rtsp_port_defaults_to_554(patched_api) -> None:
    _set_static_status_payload({"name": "D", "WIFI": {}, "local_rtsp_port": None})
    out = patched_api.get_static_status("SER")
    assert out["local_rtsp_port"] == "554"


def test_get_static_status_does_not_request_alarm_refresh(patched_api) -> None:
    """Static poll routes through ``status_static_dict``, which builds
    the dict from ``device_obj`` without touching ``unifiedmsg/list``.
    Verifies upstream's variadic ``status(...)`` is never called — the
    whole point of bypassing it is to be pyezvizapi-version-agnostic."""
    _set_static_status_payload({"name": "D", "WIFI": {}})
    patched_api.get_static_status("SER")
    cam = api_mod.Hp7EzvizCamera.return_value
    cam.status_static_dict.assert_called_once_with()
    cam.status.assert_not_called()


# ── get_alarms (phase 6.1 split) ──────────────────────────────────


def _set_alarms_payload(payload: dict) -> None:
    """Helper: ``Hp7EzvizCamera.status_alarm_dict(latest_alarm)`` returns
    ``payload`` (which carries the alarm fields and possibly
    ``Seconds_Last_Trigger``)."""
    api_mod.Hp7EzvizCamera.return_value.status_alarm_dict.return_value = payload


def _set_messages_response(messages: list) -> None:
    patched_client = api_mod.EzvizClient.return_value
    patched_client.get_device_messages_list.return_value = {"message": messages}


def test_get_alarms_raises_when_client_uninitialised(monkeypatch) -> None:
    api = Hp7Api(username="u")
    monkeypatch.setattr(api, "ensure_client", lambda: None)
    with pytest.raises(RuntimeError, match="cloud client not initialised"):
        api.get_alarms("SER")


def test_get_alarms_calls_unifiedmsg_only(patched_api) -> None:
    """Alarm poll hits ``get_device_messages_list`` only — no pagelist."""
    _set_alarms_payload(
        {
            "Seconds_Last_Trigger": 3,
            "last_alarm_time": "2026-05-11 10:00:00",
            "last_alarm_pic": "https://x/snap.jpg",
            "last_alarm_type_name": "Smart Detection Alarm",
        }
    )
    _set_messages_response([{"deviceSerial": "SER", "title": "Smart Detection Alarm"}])
    patched_api.get_alarms("SER")
    patched_api._client.get_device_messages_list.assert_called_once_with(
        serials="SER", limit=1, date="", end_time=""
    )
    patched_api._client.get_device_infos.assert_not_called()


def test_get_alarms_maps_alarm_fields(patched_api) -> None:
    _set_alarms_payload(
        {
            "Seconds_Last_Trigger": 5,
            "last_alarm_time": "2026-05-11 10:00:00",
            "last_alarm_pic": "https://x/snap.jpg",
            "last_alarm_type_name": "Doorbell ring",
            "last_alarm_type_code": "3001",
            "Motion_Trigger": True,
        }
    )
    _set_messages_response([{"deviceSerial": "SER", "title": "Doorbell ring"}])
    out = patched_api.get_alarms("SER")
    assert out == {
        "seconds_last_trigger": 5,
        "last_alarm_time": "2026-05-11 10:00:00",
        "last_alarm_pic": "https://x/snap.jpg",
        "alarm_name": "Doorbell ring",
        "alarm_type_code": "3001",
        "motion_trigger": True,
    }


def test_get_alarms_normalises_raw_unified_message_before_status_alarm_dict(
    patched_api,
) -> None:
    """The cloud returns raw unified messages with ``title`` / ``timeStr`` /
    ``subType`` / ``pic`` keys.  ``status_alarm_dict`` expects the
    normalised shape (``sampleName`` / ``alarmStartTimeStr`` / ``alarmType`` /
    ``picUrl``).  Without the normalisation hop in ``get_alarms``, the
    alarm dict would silently fall back to ``NoAlarm`` for every event.
    """
    raw_msg = {
        "deviceSerial": "SER",
        "title": "Your doorbell is ringing",
        "timeStr": "2026-05-20 18:32:02",
        "subType": "2701",
        "pic": "https://x/snap.jpg",
    }
    # The fixture sets ``api._client`` to a fresh MagicMock independent
    # of ``EzvizClient.return_value``, so we configure the instance's
    # client directly (the helper ``_set_messages_response`` targets the
    # class-level mock and would not be observed by the code under test).
    patched_api._client.get_device_messages_list.return_value = {"message": [raw_msg]}
    cam = api_mod.Hp7EzvizCamera.return_value
    cam._normalize_unified_message.return_value = {
        "sampleName": "Your doorbell is ringing",
        "alarmStartTimeStr": "2026-05-20 18:32:02",
        "alarmType": "2701",
        "picUrl": "https://x/snap.jpg",
    }
    _set_alarms_payload(
        {
            "Seconds_Last_Trigger": 1,
            "last_alarm_time": "2026-05-20 18:32:02",
            "last_alarm_pic": "https://x/snap.jpg",
            "last_alarm_type_name": "Your doorbell is ringing",
            "last_alarm_type_code": "2701",
        }
    )
    out = patched_api.get_alarms("SER")
    cam._normalize_unified_message.assert_called_once_with(raw_msg)
    # ``status_alarm_dict`` must receive the normalised dict, never the raw.
    args, _ = cam.status_alarm_dict.call_args
    assert args[0] is cam._normalize_unified_message.return_value
    # End-to-end shape: code surfaces alongside name.
    assert out["alarm_name"] == "Your doorbell is ringing"
    assert out["alarm_type_code"] == "2701"


def test_get_alarms_passes_none_when_no_message_for_this_serial(patched_api) -> None:
    """Empty / wrong-serial responses must NOT call ``_normalize_unified_message``
    on a ``None`` payload — the contract is ``status_alarm_dict(None)``."""
    _set_alarms_payload({"Seconds_Last_Trigger": None})
    patched_api._client.get_device_messages_list.return_value = {"message": []}
    cam = api_mod.Hp7EzvizCamera.return_value
    cam._normalize_unified_message.reset_mock()
    patched_api.get_alarms("SER")
    cam._normalize_unified_message.assert_not_called()
    args, _ = cam.status_alarm_dict.call_args
    assert args[0] is None


def test_get_alarms_ignores_messages_from_other_devices(patched_api) -> None:
    """Multi-device accounts: ``get_device_messages_list`` may return
    other serials in the payload.  The latest-for-this-serial filter
    has to drop them before normalisation."""
    _set_alarms_payload({"Seconds_Last_Trigger": None, "last_alarm_time": None})
    _set_messages_response(
        [
            {"deviceSerial": "OTHER", "title": "Some other alarm"},
            {"deviceSerial": "OTHER2", "title": "Yet another"},
        ]
    )
    patched_api.get_alarms("SER")
    # ``latest_alarm`` passed to ``status_alarm_dict`` must be ``None``.
    args, _ = api_mod.Hp7EzvizCamera.return_value.status_alarm_dict.call_args
    assert args == (None,)


def test_get_alarms_handles_empty_message_list(patched_api) -> None:
    _set_alarms_payload({"Seconds_Last_Trigger": None})
    _set_messages_response([])
    out = patched_api.get_alarms("SER")
    assert out["last_alarm_time"] is None
    assert out["last_alarm_pic"] is None
    assert out["alarm_name"] is None


def test_get_alarms_handles_messages_key_variant(patched_api) -> None:
    """The cloud occasionally returns ``messages`` (plural) instead of
    ``message`` — handle both."""
    _set_alarms_payload(
        {"last_alarm_time": "2026-05-11 09:00:00", "last_alarm_type_name": "Ring"}
    )
    patched_api._client.get_device_messages_list.return_value = {
        "messages": [{"deviceSerial": "SER", "title": "Ring"}]
    }
    out = patched_api.get_alarms("SER")
    assert out["last_alarm_time"] == "2026-05-11 09:00:00"
    assert out["alarm_name"] == "Ring"


def test_get_alarms_creates_camera_with_offline_device_obj(patched_api) -> None:
    """The camera helper must be instantiated with a non-empty but
    minimal ``device_obj`` so older pyezvizapi versions (which treat
    a falsy ``device_obj`` as "go fetch pagelist") don't issue a second
    HTTP request under the hood.  ``{"SWITCH": []}`` is the smallest
    payload that's truthy and satisfies legacy ``_switch`` parsing."""
    _set_alarms_payload({"Seconds_Last_Trigger": None})
    _set_messages_response([{"deviceSerial": "SER", "title": "x"}])
    patched_api.get_alarms("SER")
    args, kwargs = api_mod.Hp7EzvizCamera.call_args
    assert args == (patched_api._client, "SER")
    assert kwargs == {"device_obj": {"SWITCH": []}}


# ── Hp7EzvizCamera (real subclass, no mocks) ──────────────────────
#
# These tests exercise the actual subclass against the installed
# pyezvizapi to catch breakage from upstream signature changes —
# the entire reason for the subclass exists.


def _make_camera(device_obj: dict) -> Hp7EzvizCamera:
    """Build a real ``Hp7EzvizCamera`` with a stub client.

    The client is only touched by paths we don't hit here (alarm
    fetch, ptz, etc.); ``MagicMock`` is enough.
    """
    return Hp7EzvizCamera(MagicMock(), "SERIAL", device_obj=device_obj)


def test_hp7_camera_status_static_dict_extracts_pagelist_fields() -> None:
    cam = _make_camera(
        {
            "deviceInfos": {
                "name": "Doorbell",
                "version": "V5.3.6 build 250825",
                "status": 1,
            },
            "UPGRADE": {"isNeedUpgrade": 0},
            "CONNECTION": {"netIp": "203.0.113.1", "localRtspPort": "8554"},
            "WIFI": {"ssid": "Casa", "signal": 80, "address": "192.168.1.10"},
            "SWITCH": [],
        }
    )
    out = cam.status_static_dict()
    assert out == {
        "name": "Doorbell",
        "version": "V5.3.6 build 250825",
        "upgrade_available": False,
        "status": 1,
        "wan_ip": "203.0.113.1",
        "WIFI": {"ssid": "Casa", "signal": 80, "address": "192.168.1.10"},
        "local_ip": "192.168.1.10",
        "local_rtsp_port": "8554",
        "image_encryption": False,
    }


def test_hp7_camera_status_static_dict_normalises_zero_rtsp_port() -> None:
    """Some devices report ``localRtspPort = 0`` (or "0"); upstream
    treats those as "unset" and falls back to 554.  We do the same."""
    cam = _make_camera(
        {
            "deviceInfos": {},
            "UPGRADE": {},
            "CONNECTION": {"localRtspPort": 0},
            "SWITCH": [],
        }
    )
    assert cam.status_static_dict()["local_rtsp_port"] == "554"


def test_hp7_camera_status_static_dict_upgrade_available_when_status_3() -> None:
    cam = _make_camera({"UPGRADE": {"isNeedUpgrade": 3}, "SWITCH": []})
    assert cam.status_static_dict()["upgrade_available"] is True


def test_hp7_camera_status_alarm_dict_with_prefetched_alarm() -> None:
    """A fresh alarm (timestamp <60s ago) populates all five fields and
    a non-None ``Seconds_Last_Trigger``."""
    import datetime as _dt

    now_str = _dt.datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    cam = _make_camera({"SWITCH": []})
    out = cam.status_alarm_dict(
        {
            "alarmStartTimeStr": now_str,
            "picUrl": "https://x/snap.jpg",
            "sampleName": "Doorbell ring",
            "alarmType": "2701",
        }
    )
    assert out["last_alarm_time"] == now_str
    assert out["last_alarm_pic"] == "https://x/snap.jpg"
    assert out["last_alarm_type_name"] == "Doorbell ring"
    assert out["last_alarm_type_code"] == "2701"
    # ``timepassed`` is in seconds and should be small (just-now).
    assert out["Seconds_Last_Trigger"] is not None
    assert out["Seconds_Last_Trigger"] < 5


def test_hp7_camera_status_alarm_dict_with_none_alarm() -> None:
    """No prefetched alarm → all six fields take defaults and
    ``Seconds_Last_Trigger`` / ``Motion_Trigger`` stay ``None`` /
    falsy (no motion)."""
    cam = _make_camera({"SWITCH": []})
    out = cam.status_alarm_dict(None)
    assert out == {
        "Seconds_Last_Trigger": None,
        "last_alarm_time": None,
        "last_alarm_pic": DEFAULT_ALARM_PIC_URL,
        "last_alarm_type_name": "NoAlarm",
        "last_alarm_type_code": "0000",
        # ``compute_motion_from_alarm`` initialises ``alarm_trigger_active``
        # to ``False`` even when no alarm has been seen, so ``Motion_Trigger``
        # surfaces as ``False`` (off) rather than ``None``.
        "Motion_Trigger": False,
    }


def test_hp7_camera_status_alarm_dict_swallows_unparseable_timestamp() -> None:
    """Upstream's ``_motion_trigger`` parses ``alarmStartTimeStr`` with a
    strict format; if the cloud ever sends a variant we don't know
    about, we must NOT fail the whole poll — the alarm dict has to
    come back populated even when the motion-timing math gives up."""
    cam = _make_camera({"SWITCH": []})
    out = cam.status_alarm_dict({"alarmStartTimeStr": "garbage"})
    # No crash; the raw timestamp string is preserved for the entity,
    # and the rest of the dict still has its six keys.
    assert out["last_alarm_time"] == "garbage"
    assert set(out.keys()) == {
        "Seconds_Last_Trigger",
        "last_alarm_time",
        "last_alarm_pic",
        "last_alarm_type_name",
        "last_alarm_type_code",
        "Motion_Trigger",
    }


# ── close ──────────────────────────────────────────────────────────


def test_close_logs_out_and_clears_client(patched_api) -> None:
    client = patched_api._client
    patched_api.close()
    client.logout.assert_called_once()
    assert patched_api._client is None


def test_close_swallows_logout_errors(patched_api) -> None:
    patched_api._client.logout.side_effect = PyEzvizError("offline")
    patched_api.close()  # no raise
    assert patched_api._client is None


def test_close_is_a_noop_without_client() -> None:
    api = Hp7Api(username="u")
    api.close()  # no raise
    assert api._client is None


# ── token property ─────────────────────────────────────────────────


def test_token_property_returns_stored(fake_token) -> None:
    api = Hp7Api(username="u", token=fake_token)
    assert api.token is fake_token
