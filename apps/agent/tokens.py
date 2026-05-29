"""HMAC-signed invite codes + LiveKit JWT minting.

Fixes voicehook-v3#52 (/api/token mints canPublish tokens without auth).

Invite code shape:
    <b64u(room)>.<b64u(exp_unix)>.<b64u(hmac_sig)>

The signature is HMAC-SHA256 over "<room>|<exp>" using INVITE_SECRET. The
server-side /api/token endpoint requires a valid, unexpired invite before
minting a LiveKit JWT — strangers with a leaked room slug cannot publish.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def mint_invite(room: str, ttl_seconds: int = 3600, *, secret: str | None = None, now: int | None = None) -> str:
    """Issue an invite for the given room. Default TTL = 1h."""
    secret = secret or os.environ["INVITE_SECRET"]
    if not room or "|" in room or len(room) > 200:
        raise ValueError("invalid room slug")
    exp = (now if now is not None else int(time.time())) + ttl_seconds
    msg = f"{room}|{exp}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return f"{_b64u(room.encode('utf-8'))}.{_b64u(str(exp).encode('ascii'))}.{_b64u(sig)}"


@dataclass(frozen=True)
class InviteVerdict:
    valid: bool
    reason: str = ""
    room: str = ""
    exp: int = 0


def verify_invite(code: str, expected_room: str, *, secret: str | None = None, now: int | None = None) -> InviteVerdict:
    """Constant-time verify. Returns verdict; never raises for bad input."""
    secret = secret or os.environ["INVITE_SECRET"]
    try:
        parts = code.split(".")
        if len(parts) != 3:
            return InviteVerdict(False, "malformed")
        room = _b64u_decode(parts[0]).decode("utf-8")
        exp = int(_b64u_decode(parts[1]).decode("ascii"))
        sig = _b64u_decode(parts[2])
    except (ValueError, UnicodeDecodeError):
        return InviteVerdict(False, "malformed")

    expected_sig = hmac.new(secret.encode(), f"{room}|{exp}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        return InviteVerdict(False, "bad signature")
    if room != expected_room:
        return InviteVerdict(False, "room mismatch", room=room, exp=exp)
    cur = now if now is not None else int(time.time())
    if cur > exp:
        return InviteVerdict(False, "expired", room=room, exp=exp)
    return InviteVerdict(True, "", room=room, exp=exp)


# ----- LiveKit JWT --------------------------------------------------------

def mint_livekit_token(
    *,
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    can_publish: bool = True,
    can_subscribe: bool = True,
    ttl_seconds: int = 3600,
    now: int | None = None,
) -> str:
    """Mint a minimal LiveKit JWT (HS256) for joining `room` as `identity`."""
    import json

    nbf = (now if now is not None else int(time.time())) - 30
    exp = nbf + 30 + ttl_seconds
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": api_key,
        "sub": identity,
        "nbf": nbf,
        "exp": exp,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "canPublishData": True,
        },
    }
    h_b64 = _b64u(json.dumps(header, separators=(",", ":")).encode())
    p_b64 = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = hmac.new(api_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{h_b64}.{p_b64}.{_b64u(sig)}"
