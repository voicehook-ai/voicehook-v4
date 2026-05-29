"""Unit tests for HMAC invite codes + LK JWT mint."""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import time

import pytest

from agent.tokens import mint_invite, mint_livekit_token, verify_invite


SECRET = "test-secret-do-not-use"


def test_roundtrip_valid():
    code = mint_invite("room-x", ttl_seconds=300, secret=SECRET)
    v = verify_invite(code, "room-x", secret=SECRET)
    assert v.valid is True
    assert v.room == "room-x"
    assert v.reason == ""


def test_wrong_room_rejected():
    code = mint_invite("room-x", secret=SECRET)
    v = verify_invite(code, "room-y", secret=SECRET)
    assert v.valid is False
    assert v.reason == "room mismatch"


def test_expired_rejected():
    now = int(time.time())
    code = mint_invite("room-x", ttl_seconds=60, secret=SECRET, now=now)
    v = verify_invite(code, "room-x", secret=SECRET, now=now + 61)
    assert v.valid is False
    assert v.reason == "expired"


def test_tampered_signature_rejected():
    code = mint_invite("room-x", secret=SECRET)
    head, _, _ = code.rpartition(".")
    tampered = head + "." + base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode()
    v = verify_invite(tampered, "room-x", secret=SECRET)
    assert v.valid is False
    assert v.reason == "bad signature"


def test_wrong_secret_rejected():
    code = mint_invite("room-x", secret=SECRET)
    v = verify_invite(code, "room-x", secret="other-secret")
    assert v.valid is False
    assert v.reason == "bad signature"


def test_malformed_code_rejected():
    for bad in ["", "x", "x.y", "no.no.no.no", "...", "@@@.@@@.@@@"]:
        v = verify_invite(bad, "room-x", secret=SECRET)
        assert v.valid is False, f"should reject {bad!r}"


def test_invite_rejects_pipe_in_room():
    with pytest.raises(ValueError):
        mint_invite("evil|injection", secret=SECRET)


def test_livekit_token_is_well_formed_jwt():
    tok = mint_livekit_token(
        api_key="API_TEST",
        api_secret="secret_test_value",
        room="room-x",
        identity="alice",
        ttl_seconds=600,
    )
    parts = tok.split(".")
    assert len(parts) == 3
    # decode payload
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    payload = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
    assert payload["iss"] == "API_TEST"
    assert payload["sub"] == "alice"
    assert payload["video"]["room"] == "room-x"
    assert payload["video"]["roomJoin"] is True
    assert payload["video"]["canPublish"] is True
    assert payload["exp"] - payload["nbf"] >= 600


def test_livekit_token_signature_matches_secret():
    tok = mint_livekit_token(
        api_key="K", api_secret="S", room="r", identity="i", ttl_seconds=60
    )
    h, p, s = tok.split(".")
    expected = hmac.new(b"S", f"{h}.{p}".encode("ascii"), hashlib.sha256).digest()
    pad = lambda x: x + "=" * (-len(x) % 4)  # noqa: E731
    assert hmac.compare_digest(base64.urlsafe_b64decode(pad(s)), expected)
