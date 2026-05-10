"""Tests for the ``<Sign>`` extraction from the cloud JWT.

The CAS server's ``DirectConnectReq`` (cmd 0x2001) requires
``<Sign>`` to equal the ``s`` claim of the JWT in
``token["session_id"]``.  Anything else and the server returns a
garbled AES key.  Verify the parser handles real JWTs, malformed
JWTs and missing tokens without raising.
"""

from __future__ import annotations

import base64
import json

from custom_components.ezviz_hp7.pylocalapi.cas import _extract_sign_from_jwt


def _make_jwt(payload: dict) -> str:
    """Build a JWT-shaped string with the given payload (signature ignored)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


def test_extracts_s_claim() -> None:
    fc = "abc123def456"
    token = {"session_id": _make_jwt({"s": fc, "u": "user"})}
    assert _extract_sign_from_jwt(token) == fc


def test_falls_back_on_malformed_jwt() -> None:
    """Pre-JWT token formats — return the raw value so the server
    can still validate (it'll reject, same failure mode as before)."""
    token = {"session_id": "not-a-jwt"}
    assert _extract_sign_from_jwt(token) == "not-a-jwt"


def test_handles_missing_session_id() -> None:
    assert _extract_sign_from_jwt({}) == ""


def test_handles_jwt_without_s_claim() -> None:
    """The JWT parses fine but has no ``s`` claim — fall back to raw."""
    jwt = _make_jwt({"u": "user", "iat": 0})
    token = {"session_id": jwt}
    # No ``s`` claim → fall back to the raw JWT (CAS will reject it,
    # but the helper itself should not raise).
    assert _extract_sign_from_jwt(token) == jwt
