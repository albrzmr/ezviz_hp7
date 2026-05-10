"""Tests for ``custom_components.ezviz_hp7.pylocalapi.cas``.

Phase 1.5 of the testing plan — covers the pure-helper functions
(packet framing, AES inner encryption) plus the two ``EzvizCAS``
public methods exercised with a mocked ``_send_and_recv`` so no
network or socket is involved.

The JWT helper ``_extract_sign_from_jwt`` is intentionally NOT
re-tested here — ``tests/test_cas_jwt.py`` already covers it.
"""

from __future__ import annotations

import hashlib
import struct
from unittest.mock import patch

import pytest
from pyezvizapi.exceptions import PyEzvizError

from custom_components.ezviz_hp7.pylocalapi import cas as cas_mod
from custom_components.ezviz_hp7.pylocalapi.cas import (
    AES_IV_INNER,
    CAS_HEADER_SIZE,
    CAS_MAGIC,
    CAS_TAIL_SIZE,
    CAS_VERSION,
    EzvizCAS,
    _aes128_cbc_decrypt,
    _aes128_cbc_encrypt,
    _build_cas_packet,
    _make_cas_header,
    _make_cas_tail,
    _parse_cas_header,
    _recv_exact,
)

# ── _make_cas_header / _parse_cas_header round-trip ────────────────


def test_make_cas_header_round_trip() -> None:
    hdr = _make_cas_header(cmd=0x2001, body_len=42, session_id=0xCAFEBABE, extra=7)
    assert len(hdr) == CAS_HEADER_SIZE
    parsed = _parse_cas_header(hdr)
    assert parsed["session_id"] == 0xCAFEBABE
    assert parsed["cmd"] == 0x2001
    assert parsed["body_len"] == 42
    assert parsed["extra"] == 7
    assert parsed["flags"] == 0


def test_make_cas_header_random_session_id_in_range() -> None:
    """When ``session_id=None`` a 32-bit value must be produced."""
    hdr = _make_cas_header(cmd=1, body_len=0)
    parsed = _parse_cas_header(hdr)
    assert 1 <= parsed["session_id"] <= 0xFFFFFFFF


def test_make_cas_header_starts_with_magic_and_version() -> None:
    hdr = _make_cas_header(cmd=1, body_len=0, session_id=1)
    assert hdr[:4] == CAS_MAGIC
    assert hdr[4:8] == CAS_VERSION


def test_parse_cas_header_rejects_bad_length() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        _parse_cas_header(b"\x00" * 10)


def test_parse_cas_header_rejects_bad_magic() -> None:
    bogus = b"\xff" * 32
    with pytest.raises(ValueError, match="bad magic"):
        _parse_cas_header(bogus)


# ── _make_cas_tail (MD5) ───────────────────────────────────────────


def test_make_cas_tail_known_vector() -> None:
    body = b"hello world"
    expected = hashlib.md5(body).hexdigest().encode("ascii")
    assert _make_cas_tail(body) == expected
    assert len(_make_cas_tail(b"")) == CAS_TAIL_SIZE  # 32 hex chars even for empty


# ── _build_cas_packet ──────────────────────────────────────────────


def test_build_cas_packet_layout() -> None:
    body = b"BODY"
    pkt = _build_cas_packet(cmd=0x2001, body=body, session_id=1)
    assert len(pkt) == CAS_HEADER_SIZE + len(body) + CAS_TAIL_SIZE
    assert pkt[:4] == CAS_MAGIC
    # Body starts immediately after the header
    assert pkt[CAS_HEADER_SIZE : CAS_HEADER_SIZE + len(body)] == body
    # Tail is the MD5 of the body
    assert pkt[-CAS_TAIL_SIZE:] == _make_cas_tail(body)


# ── AES-128-CBC round-trip ─────────────────────────────────────────


def test_aes128_cbc_round_trip() -> None:
    key = b"0123456789ABCDEF"  # 16 bytes
    plaintext = (
        b"<?xml version='1.0'?><Request><OperationCode>XYZ</OperationCode></Request>"
    )
    ct = _aes128_cbc_encrypt(key, plaintext)
    assert ct != plaintext
    # CBC output is a multiple of the AES block size.
    assert len(ct) % 16 == 0
    assert _aes128_cbc_decrypt(key, ct) == plaintext


def test_aes128_iv_constant_shape() -> None:
    """The inner IV is documented as a fixed 16-byte value."""
    assert len(AES_IV_INNER) == 16


# ── _recv_exact ────────────────────────────────────────────────────


class _FakeSock:
    """Minimal socket-like object — feeds canned chunks then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def recv(self, n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_recv_exact_happy_path() -> None:
    sock = _FakeSock([b"AB", b"CD", b"EF"])
    assert _recv_exact(sock, 6) == b"ABCDEF"


def test_recv_exact_raises_when_connection_closes_early() -> None:
    sock = _FakeSock([b"AB"])
    with pytest.raises(OSError, match=r"connection closed after 2/6"):
        _recv_exact(sock, 6)


# ── EzvizCAS.__init__ ─────────────────────────────────────────────


def test_init_raises_without_service_urls() -> None:
    with pytest.raises(PyEzvizError, match="Missing service_urls"):
        EzvizCAS({"session_id": "x"})


def test_init_raises_for_none_token() -> None:
    with pytest.raises(PyEzvizError, match="Missing service_urls"):
        EzvizCAS(None)


def test_init_stores_service_urls(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    assert cas._service_urls is fake_token["service_urls"]


# ── _get_cas_host_port ─────────────────────────────────────────────


def test_get_cas_host_port_reads_sysconf_indices(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    host, port = cas._get_cas_host_port()
    # Matches the fixture (sysConf[15]/[16]).
    assert host == "cas.example.com"
    assert port == "6500"


# ── cas_get_encryption ─────────────────────────────────────────────


def test_cas_get_encryption_parses_xml(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    canned_xml = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b"<Response>"
        b"<Result>0</Result>"
        b'<Session Key="0123456789ABCDEF" OperationCode="OPCODE-1"/>'
        b"</Response>"
    )
    with patch.object(cas, "_send_and_recv", return_value=({}, canned_xml)) as send:
        out = cas.cas_get_encryption("DEV123")
    # The dict shape ``Hp7Api.fetch_lan_aes_key`` consumes.
    session = out["Response"]["Session"]
    assert session["@Key"] == "0123456789ABCDEF"
    assert session["@OperationCode"] == "OPCODE-1"
    # The XML body must carry our serial in <DevSerial>.
    sent_cmd, sent_body = send.call_args.kwargs["cmd"], send.call_args.kwargs["body"]
    assert sent_cmd == 0x2001
    assert b"<DevSerial>DEV123</DevSerial>" in sent_body
    assert b"<ClientType>3</ClientType>" in sent_body


# ── query_permanent_password ───────────────────────────────────────


_AES_KEY = b"0123456789ABCDEF"


def _build_inner_response(plain_xml: bytes, encrypted: bool = True) -> bytes:
    """Build a fake CAS inner packet the way the server would for cmd 0x2845."""
    if encrypted:
        body = _aes128_cbc_encrypt(_AES_KEY, plain_xml)
        flags = 0xFFFFFFFF
    else:
        body = plain_xml
        flags = 0

    hdr = (
        CAS_MAGIC
        + CAS_VERSION
        + struct.pack(">I", 1)  # session_id
        + b"\x00" * 4  # gap
        + struct.pack(">I", 0x2845)  # cmd
        + struct.pack(">I", flags)  # flags
        + struct.pack(">I", len(body))  # body_len
        + struct.pack(">I", 0)  # extra
    )
    tail = _make_cas_tail(body)
    return hdr + body + tail


def _build_outer_response(outer_xml: bytes, inner_pkt: bytes) -> tuple[dict, bytes]:
    """Build the (hdr, body) tuple ``_send_and_recv`` would return."""
    return (
        {"extra": len(outer_xml), "body_len": len(outer_xml) + len(inner_pkt)},
        outer_xml + inner_pkt,
    )


def test_query_permanent_password_happy_path(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    plain = (
        b'<?xml version="1.0"?>'
        b"<Response>"
        b"<Result>0</Result>"
        b'<PermanentCode Key="MY-PERMANENT-PWD"/>'
        b"</Response>"
    )
    outer_xml = b'<Response Length="0"/>'
    inner_pkt = _build_inner_response(plain, encrypted=True)
    canned = _build_outer_response(outer_xml, inner_pkt)
    with patch.object(cas, "_send_and_recv", return_value=canned):
        pwd = cas.query_permanent_password("SER", "OPCODE", _AES_KEY.decode("ascii"))
    assert pwd == "MY-PERMANENT-PWD"


def test_query_permanent_password_unencrypted_branch(fake_token: dict) -> None:
    """``flags != 0xFFFFFFFF`` → body is plaintext, not AES."""
    cas = EzvizCAS(fake_token)
    plain = b'<Response><Result>0</Result><PermanentCode Key="ABC"/></Response>'
    outer_xml = b'<Response Length="0"/>'
    inner_pkt = _build_inner_response(plain, encrypted=False)
    canned = _build_outer_response(outer_xml, inner_pkt)
    with patch.object(cas, "_send_and_recv", return_value=canned):
        assert (
            cas.query_permanent_password("SER", "OP", _AES_KEY.decode("ascii")) == "ABC"
        )


def test_query_permanent_password_invalid_aes_key_length(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    with pytest.raises(PyEzvizError, match="AES key must be 16 bytes"):
        cas.query_permanent_password("SER", "OP", "too-short")


def test_query_permanent_password_response_too_short(fake_token: dict) -> None:
    """Server returns a sub-64B inner blob → bail out."""
    cas = EzvizCAS(fake_token)
    outer_xml = b'<Response Length="0"/>'
    # 60-byte fake response = below header+tail floor.
    short_inner = b"\x00" * 60
    canned = _build_outer_response(outer_xml, short_inner)
    with (
        patch.object(cas, "_send_and_recv", return_value=canned),
        pytest.raises(PyEzvizError, match="inner response too short"),
    ):
        cas.query_permanent_password("SER", "OP", _AES_KEY.decode("ascii"))


def test_query_permanent_password_result_non_zero_raises(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    plain = b"<Response><Result>5</Result></Response>"
    outer_xml = b'<Response Length="0"/>'
    canned = _build_outer_response(outer_xml, _build_inner_response(plain))
    with (
        patch.object(cas, "_send_and_recv", return_value=canned),
        pytest.raises(PyEzvizError, match="QueryPermanentPassword failed"),
    ):
        cas.query_permanent_password("SER", "OP", _AES_KEY.decode("ascii"))


def test_query_permanent_password_no_key_in_response(fake_token: dict) -> None:
    cas = EzvizCAS(fake_token)
    plain = b"<Response><Result>0</Result></Response>"  # no PermanentCode
    outer_xml = b'<Response Length="0"/>'
    canned = _build_outer_response(outer_xml, _build_inner_response(plain))
    with (
        patch.object(cas, "_send_and_recv", return_value=canned),
        pytest.raises(PyEzvizError, match="No Key in PermanentCode"),
    ):
        cas.query_permanent_password("SER", "OP", _AES_KEY.decode("ascii"))


# ── _recv_cas_response (via raw socket-shaped object) ──────────────


def test_recv_cas_response_round_trip() -> None:
    """Feed a synthetic packet through ``_recv_cas_response`` and
    verify the parsed header + body match what was sent."""
    body = b"<Response><Result>0</Result></Response>"
    pkt = _build_cas_packet(cmd=0x2001, body=body, session_id=42)
    sock = _FakeSock(
        [
            pkt[:CAS_HEADER_SIZE],
            pkt[CAS_HEADER_SIZE:-CAS_TAIL_SIZE],
            pkt[-CAS_TAIL_SIZE:],
        ]
    )
    hdr, recv_body = cas_mod._recv_cas_response(sock)
    assert hdr["cmd"] == 0x2001
    assert hdr["session_id"] == 42
    assert recv_body == body
