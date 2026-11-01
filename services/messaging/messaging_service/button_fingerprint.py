"""Keyed fingerprints of inline callback buttons (ANYM-681), consumed by the press of ANYM-676.

A fingerprint binds a later press to the callback bytes that were inspected without revealing them: the first
16 bytes of HMAC-SHA256(key, m), hex, where the key is 32 random bytes made once per collector process and held in
memory only. After a restart every fingerprint is stale and a press refuses safely. The key and the raw callback
data are never stored, logged or returned; this module is the one implementation.
"""
import hashlib
import hmac
import secrets
import struct

_KEY = None


def process_key():
    """The per-process key, generated on first use. Never log, store or return it."""
    global _KEY
    if _KEY is None:
        _KEY = secrets.token_bytes(32)
    return _KEY


def encode(chat, message_id, row, column, data):
    """The tuple (chat id, message id, row, column, callback data) as 4-byte big-endian length-prefixed fields."""
    fields = (str(int(chat)).encode(), str(int(message_id)).encode(), str(int(row)).encode(),
              str(int(column)).encode(), bytes(data))
    return b''.join(struct.pack('>I', len(field)) + field for field in fields)


def fingerprint(chat, message_id, row, column, data, *, key=None):
    """32 hex characters. `key` is for tests only; production calls use the process key."""
    return hmac.new(key if key is not None else process_key(), encode(chat, message_id, row, column, data),
                    hashlib.sha256).digest()[:16].hex()
