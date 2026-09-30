"""HTTP-surface tests for /healthz + /api/token."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.server import app
from agent.slug import SLUG_RE
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
def test_host_call_mints_fresh_server_generated_room():
    c = TestClient(app)
    r = c.post("/api/host-call", json={"identity": "host1"},
               headers={"x-forwarded-for": "10.0.0.1"})
    assert r.status_code == 200
    body = r.json()
    assert body["identity"] == "host1"
    assert body["url"] == "wss://rtc.test"
    assert body["token"].count(".") == 2
    assert SLUG_RE.match(body["room"]), f"slug {body['room']} must match client regex"


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


# ----- ensure-dispatch presence-idempotent (#47) ---------------------------
class _FakeResp:
    def __init__(self, b): self._b = b
    def read(self): return self._b


def _dispatch_calls(monkeypatch, list_body):
    import agent.server as srv
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    monkeypatch.setenv("LIVEKIT_URL", "http://127.0.0.1:7880")
    calls = []
    def fake_urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        if url.endswith("ListDispatch"):
            return _FakeResp(list_body)
        return _FakeResp(b"{}")
    monkeypatch.setattr(srv.urllib.request, "urlopen", fake_urlopen)
    return srv, calls


def test_ensure_dispatch_skips_when_voice_ai_present(monkeypatch):
    srv, calls = _dispatch_calls(monkeypatch, b'{"agent_dispatches":[{"agent_name":"voice-ai"}]}')
    srv._ensure_agent_dispatched("room-present-47", "voice-ai")
    assert any("ListDispatch" in u for u in calls)
    assert not any("CreateDispatch" in u for u in calls)  # no double dispatch


def test_ensure_dispatch_creates_when_absent(monkeypatch):
    srv, calls = _dispatch_calls(monkeypatch, b'{"agent_dispatches":[]}')
    srv._ensure_agent_dispatched("room-absent-47", "voice-ai")
    assert any("CreateDispatch" in u for u in calls)


# ----- Gemini-Live-Testmodus: eigener Worker, per Schlüssel ------------------
import base64  # noqa: E402
import json as _json  # noqa: E402

import agent.server as srv  # noqa: E402

LIVE_KEY = "live-test-key"


def _claim_agents(token: str) -> list[str]:
    body = token.split(".")[1]
    payload = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return [a.get("agentName") for a in payload.get("roomConfig", {}).get("agents", [])]


@pytest.fixture
def _no_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "_dispatch_now", lambda room, name: calls.append((room, name)))
    return calls


def _live_room(c, key=LIVE_KEY):
    return c.post("/api/admin/live-room", json={}, headers={"authorization": f"Bearer {key}"})


def test_admin_live_room_creates_invite_link_bound_to_live_worker(monkeypatch, _no_dispatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    c = TestClient(app)
    r = _live_room(c)
    assert r.status_code == 200
    body = r.json()
    room = body["room"]
    assert body["url"].startswith(f"https://voicehook.ai/r/{room}?invite=")
    assert srv._agent_for(room) == "voice-ai-live"
    # der Link ist eine normale, raumgebundene Einladung -> Token mit Live-Worker
    invite = body["url"].split("invite=", 1)[1]
    t = c.get("/api/token", params={"room": room, "identity": "olli", "invite": invite})
    assert t.status_code == 200
    assert _claim_agents(t.json()["token"]) == ["voice-ai-live"]


def test_admin_live_room_rejects_wrong_or_missing_key(monkeypatch, _no_dispatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    c = TestClient(app)
    assert _live_room(c, "falsch").status_code == 401
    assert c.post("/api/admin/live-room", json={}).status_code == 401


def test_admin_live_room_off_without_server_key(monkeypatch, _no_dispatch):
    monkeypatch.delenv("VOICEHOOK_LIVE_KEY", raising=False)
    assert _live_room(TestClient(app)).status_code == 404


def test_key_is_never_accepted_via_query_or_body(monkeypatch, _no_dispatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    c = TestClient(app)
    assert c.post(f"/api/admin/live-room?key={LIVE_KEY}", json={}).status_code == 401
    r = c.post("/api/host-call", json={"identity": "h", "live": LIVE_KEY},
               headers={"x-forwarded-for": "10.9.0.9"})
    assert _claim_agents(r.json()["token"]) == ["voice-ai"]      # host-call kennt kein live mehr


def test_operator_join_in_live_room_dispatches_live_worker_not_normal(monkeypatch, _no_dispatch):
    # Operator-CLI (invite=1) würde sonst voice-ai dispatchen -> zwei Agents im Raum
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    room = _live_room(TestClient(app)).json()["room"]
    _no_dispatch.clear()
    srv._ensure_agent_dispatched(room)            # Default-Name wie im GET-Pfad
    assert _no_dispatch == [(room, "voice-ai-live")]


def test_normal_rooms_keep_voice_ai(_no_dispatch):
    srv._ensure_agent_dispatched("irgendein-raum-XYZ1")
    assert _no_dispatch == [("irgendein-raum-XYZ1", "voice-ai")]


# ----- Re-Dispatch: Agent kommt zurück, wenn er (Leerlauf) gegangen ist -------
NOW = 2_000_000_000 * 10**9


def _disp(name="voice-ai", age_s=600, statuses=()):
    return {"id": f"AD_{name}_{age_s}", "agent_name": name,
            "state": {"created_at": str(NOW - int(age_s * 1e9)),
                      "jobs": [{"id": f"AJ_{i}", "state": {"status": s}} for i, s in enumerate(statuses)]}}


def test_dispatch_plan_skips_when_job_running():
    keep, stale = srv._dispatch_plan([_disp(statuses=["JS_RUNNING"])], "voice-ai", NOW)
    assert keep and stale == []


def test_dispatch_plan_skips_fresh_dispatch_without_job_yet():
    keep, stale = srv._dispatch_plan([_disp(age_s=5)], "voice-ai", NOW)   # #47-Rennen
    assert keep and stale == []


def test_dispatch_plan_recreates_after_agent_left():
    d = _disp(statuses=["JS_SUCCESS"])                     # Agent per Leerlauf gegangen
    keep, stale = srv._dispatch_plan([d], "voice-ai", NOW)
    assert not keep and stale == [d["id"]]


def test_dispatch_plan_recreates_old_dispatch_without_jobs():
    d = _disp(age_s=600)
    keep, stale = srv._dispatch_plan([d], "voice-ai", NOW)
    assert not keep and stale == [d["id"]]


def test_dispatch_plan_ignores_other_agent_names():
    keep, stale = srv._dispatch_plan([_disp(name="", statuses=["JS_RUNNING"])], "voice-ai", NOW)
    assert not keep and stale == []


def test_dispatch_plan_missing_status_counts_as_pending():
    d = _disp(statuses=[None])
    d["state"]["jobs"][0]["state"] = {}                    # proto3 lässt Default weg
    keep, _ = srv._dispatch_plan([d], "voice-ai", NOW)
    assert keep
