"""Phase 2.3 — coverage for ``camera.py``.

``Hp7Camera`` is a thin shell over the cpd7 relay + the cloud
snapshot endpoint.  Tests here exercise:

- supported-features wiring (HLS → STREAM, MJPEG → 0).
- ``is_streaming`` / ``available`` / ``stream_source`` invariants.
- The MJPEG vs HLS branch in ``handle_async_mjpeg_stream``.
- ``_cloud_snapshot`` happy + error paths (no URL / no token /
  HTTP non-200 / timeout / generic exception).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.camera import Camera, CameraEntityFeature

from custom_components.ezviz_hp7.camera import Hp7Camera, async_setup_entry
from custom_components.ezviz_hp7.const import (
    DOMAIN,
    LIVE_VIEW_HLS,
    LIVE_VIEW_MJPEG,
)

CAMERA_MOD = "custom_components.ezviz_hp7.camera"


# ── helpers ────────────────────────────────────────────────────────


class _FakeAioResp:
    """Minimal stand-in for an ``aiohttp`` response usable as an async
    context manager — no need to pull ``aioresponses`` in just for this."""

    def __init__(self, status: int, body: bytes | str = b"") -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _FakeAioResp:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def read(self) -> bytes:
        return self._body if isinstance(self._body, bytes) else self._body.encode()

    async def text(self) -> str:
        if isinstance(self._body, bytes):
            return self._body.decode(errors="replace")
        return self._body


def _camera(
    *,
    relay: MagicMock | None = MagicMock(),
    mode: str = LIVE_VIEW_MJPEG,
    coordinator_data: dict[str, Any] | None = None,
    api_token: dict[str, Any] | None = None,
) -> Hp7Camera:
    coord = MagicMock()
    coord.data = coordinator_data if coordinator_data is not None else {}
    coord.api = MagicMock(token=api_token, model="HP7")
    hass = MagicMock()
    cam = Hp7Camera(hass, coord, "S-1", relay, mode, stats=MagicMock())
    return cam


# ── __init__ / supported_features ─────────────────────────────────


def test_camera_unique_id_combines_domain_and_serial() -> None:
    assert _camera().unique_id == f"{DOMAIN}_S-1_camera"


def test_hls_mode_advertises_stream_feature() -> None:
    cam = _camera(mode=LIVE_VIEW_HLS)
    assert cam.supported_features == CameraEntityFeature.STREAM


def test_mjpeg_mode_does_not_advertise_stream_feature() -> None:
    cam = _camera(mode=LIVE_VIEW_MJPEG)
    assert cam.supported_features == CameraEntityFeature(0)


# ── is_streaming ──────────────────────────────────────────────────


def test_is_streaming_false_when_relay_missing() -> None:
    assert _camera(relay=None).is_streaming is False


def test_is_streaming_false_when_idle_and_not_warm() -> None:
    relay = MagicMock(has_active_viewer=False, is_warm=False)
    assert _camera(relay=relay).is_streaming is False


def test_is_streaming_true_when_viewer_attached() -> None:
    relay = MagicMock(has_active_viewer=True, is_warm=False)
    assert _camera(relay=relay).is_streaming is True


def test_is_streaming_true_when_relay_prewarmed() -> None:
    relay = MagicMock(has_active_viewer=False, is_warm=True)
    assert _camera(relay=relay).is_streaming is True


# ── available ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [1, "1", True, "online"])
def test_available_true_when_cloud_reports_online(status: Any) -> None:
    cam = _camera(coordinator_data={"status": status})
    assert cam.available is True


@pytest.mark.parametrize("status", [0, "0", "offline", None, False, "x"])
def test_available_false_when_cloud_reports_offline(status: Any) -> None:
    cam = _camera(coordinator_data={"status": status})
    assert cam.available is False


def test_available_false_when_super_reports_unavailable() -> None:
    """When the parent ``Camera.available`` says no, we must respect it."""
    cam = _camera(coordinator_data={"status": 1})
    with patch.object(
        Camera, "available", new_callable=lambda: property(lambda self: False)
    ):
        assert cam.available is False


# ── device_info ───────────────────────────────────────────────────


def test_device_info_uses_api_model() -> None:
    cam = _camera()
    cam.coordinator.api.model = "CP7"
    info = cam.device_info
    assert info["model"] == "CP7"
    assert (DOMAIN, "S-1") in info["identifiers"]


# ── stream_source (HLS) ───────────────────────────────────────────


async def test_stream_source_none_in_mjpeg_mode() -> None:
    relay = MagicMock(port=8554, url="tcp://127.0.0.1:8554")
    cam = _camera(
        relay=relay, mode=LIVE_VIEW_MJPEG, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam.stream_source() is None


async def test_stream_source_none_when_relay_missing_or_unbound() -> None:
    cam = _camera(
        relay=None, mode=LIVE_VIEW_HLS, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam.stream_source() is None

    relay = MagicMock(port=0, url="tcp://127.0.0.1:0")
    cam2 = _camera(
        relay=relay, mode=LIVE_VIEW_HLS, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam2.stream_source() is None


async def test_stream_source_none_when_lan_ip_unknown() -> None:
    relay = MagicMock(port=8554, url="tcp://127.0.0.1:8554")
    cam = _camera(relay=relay, mode=LIVE_VIEW_HLS, coordinator_data={})
    assert await cam.stream_source() is None

    cam2 = _camera(
        relay=relay, mode=LIVE_VIEW_HLS, coordinator_data={"local_ip": "0.0.0.0"}
    )
    assert await cam2.stream_source() is None


async def test_stream_source_returns_relay_url_when_ready() -> None:
    relay = MagicMock(port=8554, url="tcp://127.0.0.1:8554")
    cam = _camera(
        relay=relay, mode=LIVE_VIEW_HLS, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam.stream_source() == "tcp://127.0.0.1:8554"


# ── handle_async_mjpeg_stream ────────────────────────────────────


async def test_mjpeg_stream_defers_to_parent_in_hls_mode() -> None:
    cam = _camera(mode=LIVE_VIEW_HLS)
    sentinel = object()
    parent = AsyncMock(return_value=sentinel)
    with patch.object(Camera, "handle_async_mjpeg_stream", parent):
        out = await cam.handle_async_mjpeg_stream(MagicMock())
    assert out is sentinel
    parent.assert_awaited_once()


async def test_mjpeg_stream_returns_none_when_relay_missing() -> None:
    cam = _camera(
        relay=None, mode=LIVE_VIEW_MJPEG, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam.handle_async_mjpeg_stream(MagicMock()) is None


async def test_mjpeg_stream_returns_none_when_relay_unbound() -> None:
    relay = MagicMock(port=0, url="")
    cam = _camera(
        relay=relay, mode=LIVE_VIEW_MJPEG, coordinator_data={"local_ip": "192.0.2.10"}
    )
    assert await cam.handle_async_mjpeg_stream(MagicMock()) is None


async def test_mjpeg_stream_returns_none_when_ip_unknown() -> None:
    relay = MagicMock(port=8554, url="tcp://127.0.0.1:8554")
    cam = _camera(relay=relay, mode=LIVE_VIEW_MJPEG, coordinator_data={})
    assert await cam.handle_async_mjpeg_stream(MagicMock()) is None


async def test_mjpeg_stream_delegates_to_serve_mjpeg() -> None:
    relay = MagicMock(port=8554, url="tcp://127.0.0.1:8554")
    cam = _camera(
        relay=relay, mode=LIVE_VIEW_MJPEG, coordinator_data={"local_ip": "192.0.2.10"}
    )
    request = MagicMock()
    fake_response = MagicMock()
    with patch(
        f"{CAMERA_MOD}.serve_mjpeg", new=AsyncMock(return_value=fake_response)
    ) as sm:
        out = await cam.handle_async_mjpeg_stream(request)

    assert out is fake_response
    sm.assert_awaited_once()
    kwargs = sm.call_args.kwargs
    assert kwargs["upstream_url"] == "tcp://127.0.0.1:8554"
    assert kwargs["stats"] is cam._stats


# ── async_camera_image / _cloud_snapshot ─────────────────────────


async def test_cloud_snapshot_returns_none_when_url_missing() -> None:
    cam = _camera(coordinator_data={})
    assert await cam.async_camera_image() is None


async def test_cloud_snapshot_returns_none_when_token_missing() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token=None,
    )
    assert await cam.async_camera_image() is None


async def test_cloud_snapshot_happy_path_returns_body_bytes() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token={"access_token": "T0K3N"},
    )
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeAioResp(200, b"\xff\xd8jpegdata"))
    with patch(f"{CAMERA_MOD}.async_get_clientsession", return_value=session):
        out = await cam.async_camera_image()
    assert out == b"\xff\xd8jpegdata"
    headers = session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer T0K3N"
    assert headers["User-Agent"].startswith("EZVIZ/")


async def test_cloud_snapshot_omits_auth_header_when_no_access_token() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token={"some_other_field": "x"},
    )
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeAioResp(200, b"img"))
    with patch(f"{CAMERA_MOD}.async_get_clientsession", return_value=session):
        out = await cam.async_camera_image()
    assert out == b"img"
    assert "Authorization" not in session.get.call_args.kwargs["headers"]


async def test_cloud_snapshot_returns_none_on_http_error() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token={"access_token": "T"},
    )
    session = MagicMock()
    session.get = MagicMock(return_value=_FakeAioResp(403, "Forbidden"))
    with patch(f"{CAMERA_MOD}.async_get_clientsession", return_value=session):
        assert await cam.async_camera_image() is None


async def test_cloud_snapshot_returns_none_on_timeout() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token={"access_token": "T"},
    )
    session = MagicMock()

    def _raise_timeout(*a: Any, **kw: Any) -> None:
        raise TimeoutError("upstream slow")

    session.get = _raise_timeout
    with patch(f"{CAMERA_MOD}.async_get_clientsession", return_value=session):
        assert await cam.async_camera_image() is None


async def test_cloud_snapshot_returns_none_on_generic_exception() -> None:
    cam = _camera(
        coordinator_data={"last_alarm_pic": "https://example/snap.jpg"},
        api_token={"access_token": "T"},
    )
    session = MagicMock()

    def _raise(*a: Any, **kw: Any) -> None:
        raise RuntimeError("boom")

    session.get = _raise
    with patch(f"{CAMERA_MOD}.async_get_clientsession", return_value=session):
        assert await cam.async_camera_image() is None


# ── async_setup_entry ────────────────────────────────────────────


async def test_async_setup_entry_registers_one_camera_with_mode_from_data() -> None:
    coord = MagicMock(data={})
    relay = MagicMock(port=0, url="")
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "e": {
                "coordinator": coord,
                "serial": "S",
                "relay": relay,
                "live_view_mode": LIVE_VIEW_HLS,
                "stats": MagicMock(),
            }
        }
    }
    entry = MagicMock(entry_id="e")
    add: MagicMock = MagicMock()

    await async_setup_entry(hass, entry, add)

    add.assert_called_once()
    (entities,), _ = add.call_args
    assert len(entities) == 1
    cam = entities[0]
    assert isinstance(cam, Hp7Camera)
    assert cam.supported_features == CameraEntityFeature.STREAM


# ── webrtc placeholder ──────────────────────────────────────────


async def test_webrtc_provider_returns_none() -> None:
    cam = _camera()
    assert await cam._async_get_supported_webrtc_provider() is None
