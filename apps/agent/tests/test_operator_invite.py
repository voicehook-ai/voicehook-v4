"""Paket 7, Punkt 8: Operator-Join (GET /api/token?invite=1, /api/bridge/join) nur mit
echter HMAC-Einladung. Ungültig -> immer 403; fehlend -> Übergangsfrist (Flag 0, laut
geloggt) oder 403 (VH_REQUIRE_OPERATOR_INVITE=1)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import freetier
from agent.server import app
from agent.tokens import mint_invite

SECRET = "test-secret-do-not-use"
ROOM = "op-gate-room"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    monkeypatch.delenv("VH_REQUIRE_OPERATOR_INVITE", raising=False)
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    freetier.register_room(ROOM, "normal", [], exempt=True)
    yield


def _get(**extra):
    params = {"room": ROOM, "identity": "claude-crown-69e3", "invite": "1", **extra}
    return TestClient(app).get("/api/token", params=params)


def test_valid_operator_invite_mints_agent_token():
    r = _get(op_invite=mint_invite(ROOM, 600, secret=SECRET))
    assert r.status_code == 200, r.text
    assert r.json()["room"] == ROOM


@pytest.mark.parametrize("flag", ["0", "1"])
@pytest.mark.parametrize("bad", ["garbage", "other-room"])
def test_invalid_operator_invite_is_always_403(monkeypatch, flag, bad):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", flag)
    inv = mint_invite("other-room-ZZ99", 600, secret=SECRET) if bad == "other-room" else "garbage"
    r = _get(op_invite=inv)
    assert r.status_code == 403
    assert "invalid invite" in r.json()["detail"]


def test_expired_operator_invite_is_403():
    import time
    inv = mint_invite(ROOM, 60, secret=SECRET, now=int(time.time()) - 3600)
    assert _get(op_invite=inv).status_code == 403


def test_missing_invite_flag0_allowed_but_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="voicehook.server"):
        r = _get()
    assert r.status_code == 200, r.text
    assert any("legacy operator join without invite" in m for m in caplog.messages)


def test_missing_invite_flag1_rejected(monkeypatch):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", "1")
    r = _get()
    assert r.status_code == 403
    assert r.json()["detail"] == "operator invite required"


def test_valid_invite_passes_with_flag1(monkeypatch):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", "1")
    assert _get(op_invite=mint_invite(ROOM, 600, secret=SECRET)).status_code == 200


def test_valid_invite_not_logged_as_legacy(caplog):
    with caplog.at_level(logging.WARNING, logger="voicehook.server"):
        _get(op_invite=mint_invite(ROOM, 600, secret=SECRET))
    assert not any("legacy operator join" in m for m in caplog.messages)
