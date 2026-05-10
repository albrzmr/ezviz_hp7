"""Tests for ``custom_components.ezviz_hp7.cpd7.crypto``.

Phase 1.6 of the testing plan — covers the helpers in ``crypto.py``
that aren't already exercised by ``tests/test_decoder_helpers.py``:

* ECDH P-256 keypair shape + shared-secret symmetry,
* ``parse_ecdh_packet`` (rejects bad inputs, parses both REQ and DATA),
* ``derive_chacha20_key`` (AES-256-ECB consistency),
* ``decrypt_chacha20_packet`` (round-trip against pycryptodome),
* known-vector tests for ``transform_nonce`` / ``make_nonce_12b``.

Coverage of ``cpd7/*`` is omitted from the integration's coverage
report (it's research code with its own captured-bin tests in the
private repo), but these unit tests still guard the cryptographic
invariants from regression.
"""

from __future__ import annotations

import base64
import struct

import pytest
from Crypto.Cipher import ChaCha20
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.ezviz_hp7.cpd7.crypto import (
    ECDH_MAGIC,
    ECDH_TYPE_DATA,
    ECDH_TYPE_REQ,
    HEADER_FIXED,
    OFF_ENCRYPTED_KEY,
    OFF_MARKER,
    OFF_NONCE_RAW,
    OFF_PEER_PUBKEY,
    OFF_TYPE,
    TRAILER_SIZE,
    decrypt_chacha20_packet,
    derive_chacha20_key,
    derive_shared_secret,
    generate_ecdh_keypair,
    make_nonce_12b,
    parse_ecdh_packet,
    transform_nonce,
)

# ── generate_ecdh_keypair ──────────────────────────────────────────


def test_generate_keypair_returns_p256_priv_and_b64_pub() -> None:
    priv, pub_b64 = generate_ecdh_keypair()
    assert isinstance(priv, ec.EllipticCurvePrivateKey)
    assert isinstance(priv.curve, ec.SECP256R1)
    # base64 string decodes cleanly to a 91-byte DER SubjectPublicKeyInfo.
    pub_der = base64.b64decode(pub_b64)
    assert len(pub_der) == 91
    pub = serialization.load_der_public_key(pub_der)
    assert isinstance(pub, ec.EllipticCurvePublicKey)
    assert isinstance(pub.curve, ec.SECP256R1)


def test_keypair_generates_unique_keys() -> None:
    _, a = generate_ecdh_keypair()
    _, b = generate_ecdh_keypair()
    # Probability of collision on P-256 is astronomically low.
    assert a != b


# ── derive_shared_secret (ECDH symmetry) ───────────────────────────


def _pub_der(priv: ec.EllipticCurvePrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_ecdh_round_trip_symmetric() -> None:
    """ECDH(a,b) == ECDH(b,a) — required for the doorbell handshake."""
    a, _ = generate_ecdh_keypair()
    b, _ = generate_ecdh_keypair()
    s1 = derive_shared_secret(a, _pub_der(b))
    s2 = derive_shared_secret(b, _pub_der(a))
    assert s1 == s2
    assert len(s1) == 32


# ── derive_chacha20_key (AES-256-ECB decrypt) ──────────────────────


def test_derive_chacha20_key_matches_aes_ecb_encrypt_inverse() -> None:
    """``derive_chacha20_key`` is just AES-256-ECB *decrypt* of a 32B
    blob; verify by AES-256-ECB *encrypt* of a known plaintext and
    checking we get the original back."""
    shared = b"\x01" * 32
    plaintext = b"PURE-PYTHON-WINS!" + b"\x00" * (32 - 17)
    cipher = Cipher(algorithms.AES(shared), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(plaintext) + encryptor.finalize()
    assert len(encrypted) == 32
    assert derive_chacha20_key(shared, encrypted) == plaintext


# ── transform_nonce / make_nonce_12b (vectors) ─────────────────────


def test_transform_nonce_known_vector() -> None:
    """Empirically (and contrary to the docstring's "swaps cancel"
    claim) the byte sequence reverses on this implementation: the
    operator-precedence quirk on the ROL16 line means the two
    half-word swaps do NOT cancel.  Lock the actual observed
    behaviour so a future refactor can't silently flip it.
    """
    assert transform_nonce(b"\xaa\xbb\xcc\xdd") == b"\xdd\xcc\xbb\xaa"
    assert transform_nonce(b"\x00\x01\x02\x03") == b"\x03\x02\x01\x00"


def test_make_nonce_12b_is_4b_swap_plus_8_zeros() -> None:
    raw = b"\xde\xad\xbe\xef"
    n = make_nonce_12b(raw)
    assert n == transform_nonce(raw) + b"\x00" * 8
    assert len(n) == 12


# ── decrypt_chacha20_packet ────────────────────────────────────────


def test_decrypt_chacha20_packet_round_trip() -> None:
    """Encrypt a payload with pycryptodome and confirm our helper
    decrypts it back — exercises the IETF (12-byte nonce, counter=0
    per packet) call signature."""
    key = b"K" * 32
    nonce = b"N" * 12
    plaintext = b"the magic words are FAKE-DRINKING-HORNS-OF-VALHALLA"
    ct = ChaCha20.new(key=key, nonce=nonce).encrypt(plaintext)
    assert decrypt_chacha20_packet(key, nonce, ct) == plaintext


def test_decrypt_chacha20_packet_counter_resets_per_packet() -> None:
    """Two back-to-back packets must each decrypt independently
    (counter resets — the doorbell encrypts every packet with
    counter=0)."""
    key = b"K" * 32
    nonce = b"N" * 12
    p1 = b"frame-1"
    p2 = b"frame-2-bigger"
    c1 = ChaCha20.new(key=key, nonce=nonce).encrypt(p1)
    c2 = ChaCha20.new(key=key, nonce=nonce).encrypt(p2)
    assert decrypt_chacha20_packet(key, nonce, c1) == p1
    assert decrypt_chacha20_packet(key, nonce, c2) == p2


# ── parse_ecdh_packet ──────────────────────────────────────────────


def _build_ecdh_packet(
    *,
    pkt_type: int,
    payload: bytes = b"",
    marker: int = 0x01,
    encrypted_key: bytes = b"\xee" * 32,
    peer_pubkey: bytes = b"\xcc" * 91,
) -> bytes:
    """Build a synthetic ECDH packet matching the wire layout that
    ``parse_ecdh_packet`` expects.

    The header section is exactly ``HEADER_FIXED`` bytes regardless
    of packet type.  REQ packets use the encrypted_key + peer_pubkey
    slots; DATA packets leave them as filler.
    """
    hdr = bytearray(b"\x00" * HEADER_FIXED)
    hdr[0] = ECDH_MAGIC
    hdr[OFF_TYPE] = pkt_type
    hdr[OFF_MARKER] = marker
    # payload_len at offset 3 — 2 bytes big-endian.
    struct.pack_into(">H", hdr, 3, len(payload))
    # 4-byte nonce at offset 7
    hdr[OFF_NONCE_RAW : OFF_NONCE_RAW + 4] = b"\x11\x22\x33\x44"
    if pkt_type == ECDH_TYPE_REQ:
        hdr[OFF_ENCRYPTED_KEY : OFF_ENCRYPTED_KEY + 32] = encrypted_key
        hdr[OFF_PEER_PUBKEY : OFF_PEER_PUBKEY + 91] = peer_pubkey
    trailer = b"\x00" * TRAILER_SIZE
    return bytes(hdr) + payload + trailer


def test_parse_ecdh_packet_too_short_returns_none() -> None:
    assert parse_ecdh_packet(b"") is None
    assert parse_ecdh_packet(b"$" + b"\x00" * 10) is None


def test_parse_ecdh_packet_wrong_magic_returns_none() -> None:
    bad = bytearray(_build_ecdh_packet(pkt_type=ECDH_TYPE_DATA))
    bad[0] = 0x00  # not '$'
    assert parse_ecdh_packet(bytes(bad)) is None


def test_parse_ecdh_packet_wrong_marker_returns_none() -> None:
    """Marker byte != 0x01 → likely an RTSP-interleaved frame, not ECDH."""
    pkt = _build_ecdh_packet(pkt_type=ECDH_TYPE_DATA, marker=0xFF)
    assert parse_ecdh_packet(pkt) is None


def test_parse_ecdh_packet_unknown_type_returns_none() -> None:
    pkt = _build_ecdh_packet(pkt_type=0x09)
    assert parse_ecdh_packet(pkt) is None


def test_parse_ecdh_packet_data_shape() -> None:
    payload = b"ENCRYPTED-VIDEO-CHUNK"
    pkt = _build_ecdh_packet(pkt_type=ECDH_TYPE_DATA, payload=payload)
    parsed = parse_ecdh_packet(pkt)
    assert parsed is not None
    assert parsed["pkt_type"] == ECDH_TYPE_DATA
    assert parsed["nonce_raw"] == b"\x11\x22\x33\x44"
    assert parsed["payload"] == payload
    assert len(parsed["trailer"]) == TRAILER_SIZE
    # DATA packets do not carry the handshake fields.
    assert "encrypted_key" not in parsed
    assert "peer_pubkey" not in parsed


def test_parse_ecdh_packet_req_shape() -> None:
    enc_key = b"\xab" * 32
    pub_b64_priv, _ = generate_ecdh_keypair()
    real_pubkey_der = _pub_der(pub_b64_priv)
    pkt = _build_ecdh_packet(
        pkt_type=ECDH_TYPE_REQ,
        encrypted_key=enc_key,
        peer_pubkey=real_pubkey_der,
    )
    parsed = parse_ecdh_packet(pkt)
    assert parsed is not None
    assert parsed["pkt_type"] == ECDH_TYPE_REQ
    assert parsed["encrypted_key"] == enc_key
    assert parsed["peer_pubkey"] == real_pubkey_der
    # And the embedded pubkey is loadable as a P-256 SubjectPublicKeyInfo.
    loaded = serialization.load_der_public_key(parsed["peer_pubkey"])
    assert isinstance(loaded, ec.EllipticCurvePublicKey)


@pytest.mark.parametrize("pkt_type", [ECDH_TYPE_REQ, ECDH_TYPE_DATA])
def test_parse_ecdh_packet_payload_length_round_trip(pkt_type: int) -> None:
    """Whatever payload_len we encode must round-trip through parse."""
    payload = b"P" * 17
    pkt = _build_ecdh_packet(pkt_type=pkt_type, payload=payload)
    parsed = parse_ecdh_packet(pkt)
    assert parsed is not None
    assert parsed["payload_len"] == len(payload)
    assert parsed["payload"] == payload
