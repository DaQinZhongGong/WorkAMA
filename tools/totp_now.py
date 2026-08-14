#!/usr/bin/env python3
"""Print the current TOTP code for a base32 secret (no external deps)."""
import base64
import hmac
import hashlib
import struct
import sys
import time


def _normalize(secret: str) -> bytes:
    # Strip padding if present and add back so base64.b32decode is happy.
    cleaned = secret.upper().strip().replace("=", "")
    pad = (8 - len(cleaned) % 8) % 8
    return base64.b32decode(cleaned + ("=" * pad), casefold=True)


def totp_now(secret: str, period: int = 30, digits: int = 6) -> str:
    key = _normalize(secret)
    counter = struct.pack(">Q", int(time.time() // period))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: totp_now.py <base32_secret>", file=sys.stderr)
        sys.exit(1)
    print(totp_now(sys.argv[1]))
