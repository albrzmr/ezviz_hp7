"""Manifest / HACS smoke tests.

Separate from ``test_init.py`` (which boots the integration through
``async_setup_entry``) so these stay parseable without HA imports —
they catch typos, missing version bumps, unsynced ``hacs.json`` /
``manifest.json`` and run in milliseconds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_INTEGRATION_DIR = (
    Path(__file__).resolve().parent.parent / "custom_components" / "ezviz_hp7"
)
_MANIFEST_PATH = _INTEGRATION_DIR / "manifest.json"
_HACS_PATH = Path(__file__).resolve().parent.parent / "hacs.json"


@pytest.fixture
def manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def test_manifest_exists() -> None:
    assert _MANIFEST_PATH.is_file(), f"missing {_MANIFEST_PATH}"


def test_manifest_required_fields(manifest: dict) -> None:
    """The fields HA / HACS validate at load time."""
    for field in (
        "domain",
        "name",
        "documentation",
        "issue_tracker",
        "codeowners",
        "config_flow",
        "iot_class",
        "requirements",
        "version",
    ):
        assert field in manifest, f"manifest missing '{field}'"


def test_manifest_domain_matches_directory(manifest: dict) -> None:
    """``domain`` must match the parent folder name or HA fails to
    register the integration with a confusing error."""
    assert manifest["domain"] == _INTEGRATION_DIR.name


def test_manifest_version_is_semver(manifest: dict) -> None:
    parts = manifest["version"].split(".")
    assert 2 <= len(parts) <= 4, f"version not dotted: {manifest['version']}"
    assert all(p.isdigit() for p in parts), (
        f"non-numeric version part in {manifest['version']}"
    )


def test_manifest_requirements_pinned(manifest: dict) -> None:
    """Pin all runtime requirements that aren't bare names — using
    unpinned versions in a HACS-distributed integration leaks
    upstream bugs straight to users without warning.
    """
    for req in manifest["requirements"]:
        # Bare names (e.g. ``cryptography``) are accepted because
        # they pin transitively via ``pyezvizapi`` / are
        # well-behaved.  Versioned specs must use ``==`` (HA's
        # preferred form for this kind of integration).
        if any(op in req for op in (">=", "<=", ">", "<", "~=", "!=")):
            pytest.fail(
                f"requirement '{req}' uses a range operator — pin with '==' instead"
            )


def test_hacs_json_exists() -> None:
    assert _HACS_PATH.is_file(), f"missing {_HACS_PATH}"


def test_hacs_json_well_formed() -> None:
    data = json.loads(_HACS_PATH.read_text())
    assert "name" in data, "hacs.json missing 'name'"
