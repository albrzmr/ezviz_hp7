"""Pure-Python LAN streaming pipeline for the HP7/CP7 doorbell.

Vendored and adapted from docs/cpd7-stream-recipe in the source repo.
The flow is:
  1. ``Cpd7LanClient.start()``      INIT/INVITE/PLAY on ports 9010+9020,
                                    encrypted with AES-128-CBC using the
                                    AES key obtained from the EUCAS server.
                                    Generates an ephemeral ECDH P-256 keypair
                                    and embeds the pubkey in the InviteStream.
  2. ``Cpd7LanClient.read_chunk()`` blocking recv from the play socket.
  3. ``StreamDecoder.feed(raw)``    parses RTSP-Interleaved chunks, derives
                                    the per-session ChaCha20 key from the
                                    first ``$\x01`` handshake, and decrypts
                                    every subsequent ``$\x02`` data packet.
  4. ``StreamDecoder.take()``       returns accumulated MPEG-PS bytes,
                                    starting at the first pack header
                                    (``00 00 01 BA``).

Crypto is centralised in ``crypto.py``.
"""
from .lan_client import Cpd7LanClient
from .decoder import StreamDecoder

__all__ = ["Cpd7LanClient", "StreamDecoder"]
