"""Helper utilities for the EZVIZ HP7 integration.

Centralises the small bits of metadata (currently just ``DeviceInfo``)
that every entity needs, so they all agree on the same ``identifiers``,
display name and model string.  Without this each platform built its
own ``DeviceInfo`` literal and they drifted apart — most hardcoded
``"HP7"`` while the camera and select used the dynamic value from the
API client, leading to a single doorbell appearing under two different
device cards in the UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .api import Hp7Api


def bare_serial(serial: str) -> str:
    """Return the main-device half of a HP7/CP7 serial.

    HP7 / CP7 stores its serial in HA's config entry as
    ``MAINSERIAL-CAMSERIAL`` (a composite of the main doorbell unit
    and the camera module).  Cloud calls keyed by device want the
    main half; ``RelatedDevice`` wants the camera half.  This helper
    returns the main half — everything before the first ``-`` — or
    the input unchanged when no hyphen is present.
    """
    return serial.split("-", 1)[0]


def get_device_info(serial: str, api: Hp7Api | None = None) -> DeviceInfo:
    """Return the canonical ``DeviceInfo`` for a doorbell.

    Args:
        serial: Device serial as stored in the config entry (may carry
            the ``MAINSERIAL-CAMSERIAL`` form for HP7 / CP7).
        api: Optional API client.  When provided, its ``model``
            attribute (set by ``Hp7Api.detect_capabilities``) is used
            so cameras detected as CP7 don't show up as "EZVIZ HP7".

    Returns:
        ``DeviceInfo`` with identifiers stable across platforms so HA
        groups every entity under one device card.
    """
    model = "HP7"
    if api is not None:
        api_model = getattr(api, "model", None)
        if api_model:
            model = api_model
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"EZVIZ {model} ({serial})",
        manufacturer="EZVIZ",
        model=model,
    )
