"""pytest-homeassistant-custom-component bootstrap + shared fixtures.

Replaces the previous stub trick (which fake-loaded
``custom_components.ezviz_hp7`` so pure-Python helpers could be
imported without HA on the path).  Now that
``pytest-homeassistant-custom-component`` is a dev-dep, we let HA
boot for real and reuse its ``hass`` / ``enable_custom_integrations``
fixtures so platform tests can do round-trip ``async_setup_entry`` /
``async_unload_entry`` checks.

Shared fixtures (``fake_jwt``, ``fake_token``, ``mock_config_entry``,
``cam_status``, ``patched_api``) live here so any ``test_*.py`` file
can pull them by name without an explicit import.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# Re-export HA's pytest plugin under the project root so its fixtures
# (``hass``, ``aioclient_mock``, ``mock_config_flow``, ``enable_custom_integrations``…)
# are loaded automatically for every test file.
pytest_plugins = ("pytest_homeassistant_custom_component",)

# Make the repo root importable so tests can do
# ``from custom_components.ezviz_hp7.api import Hp7Api``.  HA's plugin
# already adds ``custom_components`` to its loader, but this is still
# needed for non-HA imports during collection.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Opt every test into loading our integration via HA's plugin."""
    yield


@pytest.fixture
def integration_dir() -> Path:
    return _ROOT / "custom_components" / "ezviz_hp7"


# ── Reusable fixtures (Phase 0.2) ──────────────────────────────────

SERIAL = "TEST00001-TEST00001"
FEATURE_CODE = "deadbeef" * 4  # 32 hex chars


def _jwt(payload: dict) -> str:
    """JWT-shaped string; signature is not validated by our code."""
    head = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{head}.{body}.sig"


@pytest.fixture
def fake_jwt() -> str:
    return _jwt({"s": FEATURE_CODE, "u": "user@example.com"})


@pytest.fixture
def fake_service_urls() -> dict:
    """The dict ``pyezvizapi`` returns under ``token['service_urls']``.

    ``sysConf`` is a fixed-size list — index 15/16 carry the CAS host
    and port, so the fixture pads up to 17 entries.
    """
    urls = ["x"] * 17
    urls[15] = "cas.example.com"
    urls[16] = "6500"
    return {"sysConf": urls, "domain": "apiieu.ezvizlife.com"}


@pytest.fixture
def fake_token(fake_jwt: str, fake_service_urls: dict) -> dict:
    return {
        "session_id": fake_jwt,
        "rf_session_id": "rf-session",
        "username": "user@example.com",
        "api_url": "apiieu.ezvizlife.com",
        "service_urls": fake_service_urls,
    }


@pytest.fixture
def mock_config_entry(fake_token: dict) -> Any:
    """A ``MockConfigEntry`` representing a fully-paired install."""
    # Local import: HA must be importable, which is only true after the
    # plugin is loaded.  Avoid importing at module scope so this file
    # is still parseable when HA isn't installed (e.g. during ruff).
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ezviz_hp7.const import CONF_FEATURE_CODE, DOMAIN

    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERIAL,
        title=f"EZVIZ HP7 / CP7 ({SERIAL})",
        data={
            "username": "user@example.com",
            "password": "secret",
            "region": "eu",
            "serial": SERIAL,
            "token": fake_token,
            CONF_FEATURE_CODE: FEATURE_CODE,
        },
        options={"live_view_mode": "mjpeg"},
    )


@pytest.fixture
def cam_status() -> dict:
    """Merged shape of ``Hp7Api.get_static_status`` + ``get_alarms``."""
    return {
        "name": "Doorbell",
        "version": "V5.3.6 build 250825",
        "upgrade_available": 0,
        "status": 1,
        "wan_ip": "203.0.113.1",
        "seconds_last_trigger": None,
        "last_alarm_time": "2026-05-10T18:47:22+00:00",
        "last_alarm_pic": "https://example/snap.jpg",
        "alarm_name": "Smart Detection Alarm",
        "ssid": "Wifi",
        "signal": 80,
        "local_ip": "192.0.2.10",
        "local_rtsp_port": "554",
    }


@pytest.fixture
def patched_api(mocker, monkeypatch, fake_token: dict):
    """``Hp7Api`` with ``EzvizClient`` / ``EzvizCamera`` / ``EzvizCAS`` mocked.

    The mocks live in the integration module's namespace (not the
    pyezvizapi package), so tests can configure return values via e.g.
    ``api_mod.EzvizCAS.return_value.cas_get_encryption.return_value =
    ...``.  Use plain ``MagicMock`` instead of ``autospec`` so tests
    can attach arbitrary attributes without falling foul of strict
    signature checking on every nested call.
    """
    mocker.patch("custom_components.ezviz_hp7.api.EzvizClient")
    mocker.patch("custom_components.ezviz_hp7.api.EzvizCamera")
    mocker.patch("custom_components.ezviz_hp7.api.EzvizCAS")

    # Snapshot the pyezvizapi globals so ``_apply_feature_code`` calls
    # in tests don't leak between cases.
    import custom_components.ezviz_hp7.api as api_mod

    monkeypatch.setattr(
        api_mod._ezviz_client_mod,
        "FEATURE_CODE",
        api_mod._ezviz_client_mod.FEATURE_CODE,
    )
    monkeypatch.setitem(
        api_mod._ezviz_client_mod.REQUEST_HEADER,
        "featureCode",
        api_mod._ezviz_client_mod.REQUEST_HEADER.get("featureCode", ""),
    )

    from custom_components.ezviz_hp7.api import Hp7Api

    api = Hp7Api(
        username="user@example.com",
        password="secret",
        region="eu",
        token=fake_token,
        feature_code=FEATURE_CODE,
    )
    # Default to a usable client so tests can override per-case.
    api._client = MagicMock()
    api._client.login.return_value = fake_token
    return api
