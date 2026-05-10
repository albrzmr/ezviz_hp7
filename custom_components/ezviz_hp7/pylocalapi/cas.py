"""pyezvizapi CAS API Functions.

EUCAS protocol: TLS to CAS server → framed packets → XML commands.

Protocol flow:
  1. cmd 0x2001 DirectConnect → get OperationCode + AES Key per device
  2. cmd 0x2845 QueryPermanentPassword → get permanent_password for LAN auth
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import socket
import ssl
import struct
from typing import Any, cast

import xmltodict
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from pyezvizapi.exceptions import PyEzvizError

_LOGGER = logging.getLogger(__name__)


def _extract_sign_from_jwt(token: dict[str, Any]) -> str:
    """Return the ``s`` (featureCode) claim from the JWT in ``token``.

    The CAS server validates the ``<Sign>`` field against the
    featureCode the cloud login was performed with — i.e. against the
    JWT's ``s`` claim.  We extract it from the token instead of using
    a hardcoded constant, so each install fingerprints differently
    and the value is impossible for EZVIZ to blacklist globally.

    Falls back to ``session_id`` if the JWT is unparsable (older token
    formats); the server will reject it but the failure mode is the
    same as before.
    """
    raw = token.get("session_id") or ""
    try:
        # JWT format: header.payload.signature — base64url-encoded
        parts = raw.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            # Add padding if missing (base64url omits ``=`` padding)
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
            sign = data.get("s")
            if isinstance(sign, str) and sign:
                return sign
    except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _LOGGER.debug("[CAS] JWT parse failed (%s) — using raw session_id", exc)
    return str(raw)


# ── CAS protocol constants ──────────────────────────────────────────────
CAS_MAGIC = b"\x9e\xba\xac\xe9"
CAS_VERSION = b"\x01\x00\x00\x00"
CAS_HEADER_SIZE = 32
CAS_TAIL_SIZE = 32

# Fixed IV for QueryPermanentPassword inner encryption (from libezstreamclient)
AES_IV_INNER = b"01234567" + b"\x00" * 8


# ── CAS packet helpers ──────────────────────────────────────────────────


def _make_cas_header(
    cmd: int,
    body_len: int,
    session_id: int | None = None,
    flags: int = 0,
    extra: int = 0,
) -> bytes:
    """Build a 32-byte CAS protocol header.

    Layout:
      [ 0:4]  magic = 9ebaace9
      [ 4:8]  version = 01000000
      [ 8:12] sessionID (BE u32)
      [12:16] gap (zeros)
      [16:20] cmd (BE u32)
      [20:24] flags (BE u32)
      [24:28] body_len (BE u32)
      [28:32] extra (BE u32)
    """
    if session_id is None:
        session_id = random.randint(1, 0xFFFFFFFF)
    return (
        CAS_MAGIC
        + CAS_VERSION
        + struct.pack(">I", session_id)
        + b"\x00" * 4
        + struct.pack(">I", cmd)
        + struct.pack(">I", flags)
        + struct.pack(">I", body_len)
        + struct.pack(">I", extra)
    )


def _make_cas_tail(body: bytes) -> bytes:
    """Compute 32-byte ASCII-hex MD5 tail of the given body."""
    return hashlib.md5(body).hexdigest().encode("ascii")


def _build_cas_packet(
    cmd: int, body: bytes, session_id: int | None = None, extra: int = 0
) -> bytes:
    """Build a complete CAS packet: [32B header][body][32B MD5 tail]."""
    header = _make_cas_header(cmd, len(body), session_id=session_id, extra=extra)
    tail = _make_cas_tail(body)
    return header + body + tail


def _recv_exact(sock: socket.socket | ssl.SSLSocket, n: int) -> bytes:
    """Read exactly n bytes from a socket."""
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise OSError(f"connection closed after {len(out)}/{n} bytes")
        out += chunk
    return out


def _recv_cas_response(
    sock: socket.socket | ssl.SSLSocket,
) -> tuple[dict[str, Any], bytes]:
    """Read a CAS response: [32B header][body][32B tail].

    Returns (header_dict, body_bytes).
    """
    hdr_bytes = _recv_exact(sock, CAS_HEADER_SIZE)
    hdr = _parse_cas_header(hdr_bytes)
    body = _recv_exact(sock, hdr["body_len"]) if hdr["body_len"] > 0 else b""
    tail = _recv_exact(sock, CAS_TAIL_SIZE)
    expected = _make_cas_tail(body)
    if tail != expected:
        _LOGGER.debug(
            "CAS tail mismatch: got=%s expected=%s", tail.hex(), expected.hex()
        )
    return hdr, body


def _parse_cas_header(data: bytes) -> dict[str, Any]:
    """Parse a 32-byte CAS header."""
    if len(data) != CAS_HEADER_SIZE:
        raise ValueError(f"header must be 32 bytes, got {len(data)}")
    if data[:4] != CAS_MAGIC:
        raise ValueError(f"bad magic: {data[:4].hex()}")
    return {
        "session_id": struct.unpack(">I", data[8:12])[0],
        "cmd": struct.unpack(">I", data[16:20])[0],
        "flags": struct.unpack(">I", data[20:24])[0],
        "body_len": struct.unpack(">I", data[24:28])[0],
        "extra": struct.unpack(">I", data[28:32])[0],
    }


# ── AES-128-CBC (for cmd 0x2845 inner encryption) ──────────────────────


def _aes128_cbc_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC with PKCS7 padding and the fixed inner IV."""
    cipher = AES.new(key, AES.MODE_CBC, AES_IV_INNER)
    return cipher.encrypt(pad(plaintext, AES.block_size))


def _aes128_cbc_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    """AES-128-CBC decrypt with PKCS7 unpadding and the fixed inner IV."""
    cipher = AES.new(key, AES.MODE_CBC, AES_IV_INNER)
    return unpad(cipher.decrypt(ciphertext), AES.block_size)


# ── TLS connection helper ───────────────────────────────────────────────


def _cas_tls_connect(host: str, port: int, timeout: float = 10.0) -> ssl.SSLSocket:
    """Open a TLS connection to the CAS server (TLSv1.2 forced)."""
    raw = socket.create_connection((host, port), timeout=timeout)
    raw.settimeout(timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    except (AttributeError, ValueError):
        pass
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx.wrap_socket(raw, server_hostname=None)


# ── CAS client class ────────────────────────────────────────────────────


class EzvizCAS:
    """Ezviz CAS server client."""

    def __init__(self, token: dict[str, Any] | None) -> None:
        """Initialize the client object.

        Args:
            token: Authentication token from cloud login.
        """
        self._session = None
        self._token: dict[str, Any] = token or {
            "session_id": None,
            "rf_session_id": None,
            "username": None,
            "api_url": "apiieu.ezvizlife.com",
        }
        if not token or "service_urls" not in token:
            raise PyEzvizError(
                "Missing service_urls in token; call EzvizClient.login() first"
            )
        self._service_urls: dict[str, Any] = token["service_urls"]

    # ── Internal helpers ─────────────────────────────────────────────

    def _get_cas_host_port(self) -> tuple[str, int]:
        """Return (host, port) for the CAS server from service_urls."""
        host = cast("str", self._service_urls["sysConf"][15])
        port = cast("int", self._service_urls["sysConf"][16])
        return host, port

    def _send_and_recv(
        self, cmd: int, body: bytes, extra: int = 0
    ) -> tuple[dict[str, Any], bytes]:
        """Open TLS to CAS, send cmd packet, read response, close socket.

        Returns (response_header, response_body).
        """
        host, port = self._get_cas_host_port()
        sock = _cas_tls_connect(host, port)
        try:
            pkt = _build_cas_packet(cmd, body, extra=extra)
            sock.sendall(pkt)
            return _recv_cas_response(sock)
        finally:
            sock.close()

    # ── cmd 0x2001 DirectConnect (encryption key) ────────────────────

    def cas_get_encryption(self, devserial: str) -> dict[str, Any]:
        """Fetch encryption code from EZVIZ CAS server. (cmd 0x2001)

        Returns parsed XML Response with Session containing Key and OperationCode.
        """
        # ``<Sign>`` must equal the featureCode the cloud login was
        # performed with — extract it from the JWT (claim ``s``) so
        # each install fingerprints with its own per-install random
        # featureCode (no global hardcoded constant for EZVIZ to
        # blacklist).
        sign = _extract_sign_from_jwt(self._token)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<Request>"
            f"<ClientID>{self._token['session_id']}</ClientID>"
            f"<Sign>{sign}</Sign>"
            f"<DevSerial>{devserial}</DevSerial>"
            "<ClientType>3</ClientType>"
            "</Request>"
        ).encode()

        _hdr, rsp_body = self._send_and_recv(cmd=0x2001, body=body)
        _LOGGER.debug("Get Encryption Key: %r", rsp_body)
        doc = xmltodict.parse(rsp_body)
        return cast("dict[str, Any]", doc)

    # ── cmd 0x2845 QueryPermanentPassword ────────────────────────────

    def query_permanent_password(
        self, serial: str, operation_code: str, key_hex: str
    ) -> str:
        """Query permanent password via cmd 0x2845 (AES-encrypted inner).

        This is the password that works for LAN Hikvision-protocol login.
        Without it, LAN auth returns NORIGHT (0x97) for post-login commands.

        Args:
            serial: Device serial number.
            operation_code: OperationCode from cmd 0x2001 response.
            key_hex: Key (AES-128) from cmd 0x2001 response (16 ASCII chars).

        Returns:
            permanent_password string (PermanentCode Key attribute).

        Raises:
            PyEzvizError: If the query fails.
        """
        aes_key = key_hex.encode("ascii")
        if len(aes_key) != 16:
            raise PyEzvizError(
                f"AES key must be 16 bytes, got {len(aes_key)} (key_hex={key_hex!r})"
            )

        # Inner XML
        inner_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<Request>"
            f"<OperationCode>{operation_code}</OperationCode>"
            "</Request>"
        ).encode()

        # Inner packet: header(cmd=0x2845) + AES-encrypted body + MD5 tail
        inner_ciphertext = _aes128_cbc_encrypt(aes_key, inner_xml)
        inner_hdr = _make_cas_header(0x2845, len(inner_ciphertext))
        inner_tail = _make_cas_tail(inner_ciphertext)
        inner_packet = inner_hdr + inner_ciphertext + inner_tail

        # Outer XML: Transfer wrapper
        session_id = self._token.get("session_id", "")
        outer_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<Request>"
            f'<Verify ClientSession="{session_id}" ToDevice="{serial}" ClientType="3"/>'
            f'<Message Length="{len(inner_packet)}"/>'
            "</Request>"
        ).encode()

        # Outer body: XML + binary inner packet
        outer_body = outer_xml + inner_packet

        # Outer packet: header(cmd=0x2005, extra=len(outer_xml)) + outer_body + MD5 tail
        hdr, rsp_body = self._send_and_recv(
            cmd=0x2005, body=outer_body, extra=len(outer_xml)
        )

        # Parse outer response: strip XML wrapper to get inner binary
        outer_rsp_xml_len = hdr["extra"]
        rsp_xml = rsp_body[:outer_rsp_xml_len]
        _LOGGER.debug(
            "CAS outer response XML: %s", rsp_xml.decode("utf-8", errors="replace")
        )
        inner_rsp = rsp_body[outer_rsp_xml_len:]

        if len(inner_rsp) < CAS_HEADER_SIZE + CAS_TAIL_SIZE:
            raise PyEzvizError(f"inner response too short: {len(inner_rsp)}B")

        # Parse inner header and use body_len to slice correctly
        inner_rsp_hdr = _parse_cas_header(inner_rsp[:CAS_HEADER_SIZE])
        inner_body_len = inner_rsp_hdr["body_len"]
        inner_flags = inner_rsp_hdr["flags"]
        _LOGGER.debug(
            "CAS inner hdr: cmd=0x%x flags=0x%x body_len=%d inner_rsp=%dB",
            inner_rsp_hdr["cmd"],
            inner_flags,
            inner_body_len,
            len(inner_rsp),
        )

        inner_ct = inner_rsp[CAS_HEADER_SIZE : CAS_HEADER_SIZE + inner_body_len]
        inner_tail = inner_rsp[
            CAS_HEADER_SIZE + inner_body_len : CAS_HEADER_SIZE
            + inner_body_len
            + CAS_TAIL_SIZE
        ]

        # Verify inner tail
        expected_tail = _make_cas_tail(inner_ct)
        if inner_tail != expected_tail:
            _LOGGER.debug(
                "Inner tail mismatch: got=%s expected=%s",
                inner_tail.hex(),
                expected_tail.hex(),
            )

        # Decrypt inner body (flags == 0xFFFFFFFF means encrypted)
        if inner_flags == 0xFFFFFFFF:
            inner_plaintext = _aes128_cbc_decrypt(aes_key, inner_ct)
        else:
            inner_plaintext = inner_ct
        inner_text = inner_plaintext.decode("utf-8", errors="replace")
        _LOGGER.debug("CAS inner response: %s", inner_text)

        # Parse XML response
        import re

        m = re.search(r"<Result>(\d+)</Result>", inner_text)
        if not m or m.group(1) != "0":
            raise PyEzvizError(f"QueryPermanentPassword failed: {inner_text}")

        m = re.search(r'Key="([^"]+)"', inner_text)
        if not m:
            raise PyEzvizError(f"No Key in PermanentCode response: {inner_text}")

        permanent_pwd = m.group(1)
        _LOGGER.debug("Got permanent_password: %s", permanent_pwd[:4] + "…")
        return permanent_pwd
