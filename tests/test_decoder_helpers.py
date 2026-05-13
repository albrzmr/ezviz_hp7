"""Tests for the pure-Python helpers in ``cpd7.decoder`` /
``cpd7.crypto`` that don't need any HA scaffolding to exercise.

The full ``StreamDecoder`` end-to-end test would need a captured
``.bin`` from a real doorbell session and the matching ECDH private
key — those are user-specific and live outside this repo.
"""

from __future__ import annotations

from custom_components.ezviz_hp7.cpd7 import StreamDecoder
from custom_components.ezviz_hp7.cpd7.crypto import (
    make_nonce_12b,
    transform_nonce,
)
from custom_components.ezviz_hp7.cpd7.decoder import (
    H264_SPS_3B,
    H264_SPS_4B,
    HEVC_VPS_3B,
    HEVC_VPS_4B,
    MPEG_PS_PACK,
)

# ── _find_keyframe ──────────────────────────────────────────────────


def test_find_keyframe_hevc_4b_at_start() -> None:
    assert StreamDecoder._find_keyframe(HEVC_VPS_4B + b"\x00") == 0


def test_find_keyframe_hevc_4b_with_prefix() -> None:
    assert StreamDecoder._find_keyframe(b"\xaa\xbb" + HEVC_VPS_4B) == 2


def test_find_keyframe_hevc_3b_when_no_4b() -> None:
    assert StreamDecoder._find_keyframe(b"\xff\xff" + HEVC_VPS_3B) == 2


def test_find_keyframe_h264_4b_at_start() -> None:
    assert StreamDecoder._find_keyframe(H264_SPS_4B + b"\x00") == 0


def test_find_keyframe_h264_3b_with_prefix() -> None:
    assert StreamDecoder._find_keyframe(b"\xaa\xbb" + H264_SPS_3B) == 2


def test_find_keyframe_prefers_earlier_marker() -> None:
    # HEVC VPS at offset 4, H.264 SPS later — return the earlier one.
    buf = b"\x00" * 4 + HEVC_VPS_4B + b"\xff" * 8 + H264_SPS_4B
    assert StreamDecoder._find_keyframe(buf) == 4


def test_find_keyframe_returns_minus_one_when_absent() -> None:
    assert StreamDecoder._find_keyframe(b"\x00\x00\x00\x00\x00\x00") == -1


# ── nonce transform (transform_nonce + make_nonce_12b) ─────────────


def test_transform_nonce_round_trip_is_idempotent() -> None:
    """The two byte-swaps in the wire-format nonce decoder cancel for
    aligned input.  See ``cpd7/crypto.py`` for the math.
    """
    raw = b"\xaa\xbb\xcc\xdd"
    out = transform_nonce(raw)
    assert len(out) == 4
    # Should be a permutation of the same 4 bytes.
    assert sorted(out) == sorted(raw)


def test_make_nonce_12b_pads_with_zeros() -> None:
    """ChaCha20 IETF nonce is 12 bytes — last 8 must be zero."""
    n = make_nonce_12b(b"\x01\x02\x03\x04")
    assert len(n) == 12
    assert n[4:] == b"\x00" * 8


# ── MPEG-PS pack header constant sanity ───────────────────────────


def test_mpeg_ps_pack_is_4_bytes() -> None:
    assert len(MPEG_PS_PACK) == 4
    # The MPEG-PS pack-start prefix never appears in random video
    # noise — it's the specific magic ``00 00 01 BA``.
    assert MPEG_PS_PACK == b"\x00\x00\x01\xba"


# ── StreamDecoder buffer + emit semantics ─────────────────────────


def test_decoder_initial_state_is_clean() -> None:
    """A fresh decoder must report ``keys_derived == False`` and
    return nothing on ``take``.
    """
    d = StreamDecoder(ecdh_priv=None)
    assert not d.keys_derived
    assert d.take() == b""


def test_decoder_take_drains_pending_emit_buffer() -> None:
    """Direct manipulation — verifies ``take`` empties ``_out``."""
    d = StreamDecoder(ecdh_priv=None)
    d._out.extend(b"\x00\x00\x01\xbafake")
    out = d.take()
    assert out == b"\x00\x00\x01\xbafake"
    assert d.take() == b""
