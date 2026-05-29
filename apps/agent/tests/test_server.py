"""HTTP-surface tests for /healthz + /api/token."""

from __future__ import annotations

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
