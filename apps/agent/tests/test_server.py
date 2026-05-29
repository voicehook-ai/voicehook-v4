"""HTTP-surface tests for /healthz + /api/token."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from agent.server import app
from agent.tokens import mint_invite

SECRET = "test-secret-do-not-use"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    yield


def test_healthz_returns_200():
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_status_returns_probe_shape():
    c = TestClient(app)
    r = c.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "voicehook-agent"
    assert set(body["probe"].keys()) == {"stt", "tts", "llm"}
    assert isinstance(body["healthy"], bool)


def test_status_healthy_true_when_all_creds_present(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("GOOGLE_API_KEY", "y")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")
    c = TestClient(app)
    body = c.get("/status").json()
    assert body["healthy"] is True
    assert body["probe"] == {"stt": True, "tts": True, "llm": True}


def test_status_unhealthy_when_creds_missing(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    c = TestClient(app)
    body = c.get("/status").json()
    assert body["healthy"] is False


def test_api_token_rejects_without_invite():
    c = TestClient(app)
    r = c.post("/api/token", json={"room": "x", "identity": "alice", "invite": "bogus"})
    assert r.status_code == 403
    assert "invalid invite" in r.json()["detail"]


def test_api_token_rejects_wrong_room():
    c = TestClient(app)
    code = mint_invite("room-x", secret=SECRET)
    r = c.post("/api/token", json={"room": "room-y", "identity": "alice", "invite": code})
    assert r.status_code == 403
    assert "room mismatch" in r.json()["detail"]


def test_api_token_mints_with_valid_invite():
    c = TestClient(app)
    code = mint_invite("room-x", secret=SECRET)
    r = c.post("/api/token", json={"room": "room-x", "identity": "alice", "invite": code})
    assert r.status_code == 200
    body = r.json()
    assert body["room"] == "room-x"
    assert body["identity"] == "alice"
    assert body["url"] == "wss://rtc.test"
    assert body["token"].count(".") == 2  # JWT shape


def test_api_token_fails_without_livekit_creds(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    c = TestClient(app)
    code = mint_invite("room-x", secret=SECRET)
    r = c.post("/api/token", json={"room": "room-x", "identity": "alice", "invite": code})
    assert r.status_code == 503


def test_api_token_validates_payload_shape():
    c = TestClient(app)
    # missing identity
    r = c.post("/api/token", json={"room": "x", "invite": "y"})
    assert r.status_code == 422
    # empty room
    r = c.post("/api/token", json={"room": "", "identity": "alice", "invite": "y"})
    assert r.status_code == 422
    # ttl out of range
    code = mint_invite("room-x", secret=SECRET)
    r = c.post(
        "/api/token",
        json={"room": "room-x", "identity": "alice", "invite": code, "ttl_seconds": 5},
    )
    assert r.status_code == 422


# ----- /api/host-call (#20) ------------------------------------------------
_SLUG_RX = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4}$")


def test_host_call_mints_fresh_server_generated_room():
    c = TestClient(app)
    r = c.post("/api/host-call", json={"identity": "host1"},
               headers={"x-forwarded-for": "10.0.0.1"})
    assert r.status_code == 200
    body = r.json()
    assert body["identity"] == "host1"
    assert body["url"] == "wss://rtc.test"
    assert body["token"].count(".") == 2
    assert _SLUG_RX.match(body["room"]), f"slug {body['room']} must match client regex"


def test_host_call_rooms_are_unique():
    c = TestClient(app)
    rooms = {
        c.post("/api/host-call", json={"identity": "h"},
               headers={"x-forwarded-for": "10.0.0.2"}).json()["room"]
        for _ in range(5)
    }
    assert len(rooms) == 5  # server picks a fresh slug each time


def test_host_call_rate_limited_per_ip():
    c = TestClient(app)
    ip = {"x-forwarded-for": "10.0.0.3"}
    for _ in range(5):
        assert c.post("/api/host-call", json={"identity": "h"}, headers=ip).status_code == 200
    # 6th within the window is throttled
    assert c.post("/api/host-call", json={"identity": "h"}, headers=ip).status_code == 429


def test_host_call_validates_payload():
    c = TestClient(app)
    assert c.post("/api/host-call", json={}, headers={"x-forwarded-for": "10.0.0.4"}).status_code == 422


# ----- invite=1 auto-dispatches voice-ai (#42) -----------------------------
def test_invite1_auto_dispatches_voice_ai(monkeypatch):
    import time as _t

    import agent.server as srv
    calls = []
    monkeypatch.setattr(
        srv, "_ensure_agent_dispatched",
        lambda room, agent_name="voice-ai": calls.append((room, agent_name)),
    )
    c = TestClient(app)
    r = c.get("/api/token", params={"room": "auto-disp-room", "identity": "claude", "invite": "1"})
    assert r.status_code == 200
    assert r.json()["room"] == "auto-disp-room"
    # dispatch fires in a daemon thread — give it a beat
    for _ in range(50):
        if calls:
            break
        _t.sleep(0.02)
    assert calls == [("auto-disp-room", "voice-ai")]
