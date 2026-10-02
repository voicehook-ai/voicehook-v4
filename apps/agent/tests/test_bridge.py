"""HTTPS bridge (/api/bridge/*): auth, allowlist, guards, limits, SSE format.

LiveKit is mocked: FakeRoom duck-types livekit.rtc.Room (on/connect/disconnect,
remote_participants, local_participant.publish_data)."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agent import bridge
from agent import server as srv
from agent.server import app
from agent.tokens import mint_invite

SECRET = "test-secret-do-not-use"
ROOM = "blau-tiger-wald-AB12"


class FakePub:
    def __init__(self, kind: int = 1, muted: bool = False) -> None:
        self.kind = kind
        self.muted = muted


class FakePart:
    def __init__(self, identity: str, kind: int = 0, attributes: dict | None = None,
                 audio: bool = True) -> None:
        self.identity = identity
        self.kind = kind
        self.name = identity
        self.attributes = attributes or {}
        self.track_publications = {"a": FakePub()} if audio else {}


class FakeLocal:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish_data(self, data: bytes, reliable: bool = True, topic: str = "") -> None:
        self.published.append((topic, json.loads(data.decode())))


class FakePacket:
    def __init__(self, topic: str, payload: dict, sender: str) -> None:
        self.topic = topic
        self.data = json.dumps(payload).encode()
        self.participant = FakePart(sender)


class FakeRoom:
    instances: list[FakeRoom] = []
    fail_connect = False
    peers: list[FakePart] = []

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.remote_participants = {p.identity: p for p in FakeRoom.peers}
        self.local_participant = FakeLocal()
        self.connected_with: tuple | None = None
        self.disconnected = False
        FakeRoom.instances.append(self)

    def on(self, ev: str, cb=None):
        self.handlers.setdefault(ev, []).append(cb)
        return cb

    def emit(self, ev: str, *args) -> None:
        for cb in self.handlers.get(ev, []):
            cb(*args)

    async def connect(self, url: str, token: str, options=None) -> None:
        if FakeRoom.fail_connect:
            raise RuntimeError("wait_pc_connection timed out")
        self.connected_with = (url, token)

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    monkeypatch.delenv("VOICEHOOK_BRIDGE_LIVEKIT_URL", raising=False)
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    monkeypatch.setattr(bridge, "REGISTRY", bridge.Registry())
    monkeypatch.setattr(bridge, "ROOM_FACTORY", FakeRoom)
    monkeypatch.setattr(bridge, "ROOM_OPTIONS", lambda: None)
    monkeypatch.setattr(bridge, "GUARD_TICK_S", 0.05)
    FakeRoom.instances = []
    FakeRoom.fail_connect = False
    FakeRoom.peers = [FakePart("voice-ai-1", kind=4), FakePart("host-user", kind=0)]
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _join(c: TestClient, **kw) -> dict:
    body = {"invite_url": f"https://voicehook.ai/r/{ROOM}", "name": "Claude", "model": "opus-5.5"}
    body.update(kw)
    r = c.post("/api/bridge/join", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _h(sess: str) -> dict:
    return {"Authorization": f"Bearer {sess}"}


def _call(c: TestClient, fn, *args):
    """Run `fn` inside the app's event loop (LiveKit callbacks run there)."""
    return c.portal.call(fn, *args)


def _wait(pred, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


# ----- auth --------------------------------------------------------------------

def test_join_uses_cli_operator_token_and_attributes(client):
    j = _join(client)
    assert j["room"] == ROOM and j["session"] and j["expires_in"] == 3600
    room = FakeRoom.instances[-1]
    url, token = room.connected_with
    assert url == "wss://rtc.test"
    import base64
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert claims["attributes"] == {"vh.role": "agent", "vh.name": "Claude", "vh.model": "opus-5.5"}
    assert claims["video"]["room"] == ROOM and claims["sub"] == j["identity"]
    assert "roomConfig" not in claims  # plain operator token, like invite=1


def test_join_with_valid_hmac_invite(client):
    inv = mint_invite(ROOM, 600, secret=SECRET)
    j = _join(client, invite_url=f"https://voicehook.ai/r/{ROOM}?invite={inv}")
    assert j["room"] == ROOM


@pytest.mark.parametrize("bad", ["garbage", None])
def test_join_with_invalid_invite_is_403(client, bad):
    inv = bad or mint_invite("other-room-ZZ99", 600, secret=SECRET)
    r = client.post("/api/bridge/join", json={
        "invite_url": f"https://voicehook.ai/r/{ROOM}?invite={inv}", "name": "C", "model": "m"})
    assert r.status_code == 403
    assert not FakeRoom.instances


def test_join_expired_invite_is_403(client):
    inv = mint_invite(ROOM, 60, secret=SECRET, now=int(time.time()) - 3600)
    r = client.post("/api/bridge/join", json={"room": ROOM, "invite": inv, "name": "C", "model": "m"})
    assert r.status_code == 403 and "expired" in r.text


def test_join_requires_name_and_model(client):
    r = client.post("/api/bridge/join", json={"invite_url": f"https://voicehook.ai/r/{ROOM}", "name": "C"})
    assert r.status_code == 422


def test_join_ended_room_is_410_like_cli(client, monkeypatch):
    monkeypatch.setattr(srv.billing_routes.db, "room_binding", lambda room: ("acc", "x", "ended"))
    r = client.post("/api/bridge/join", json={"room": ROOM, "name": "C", "model": "m"})
    assert r.status_code == 410


def test_join_connect_failure_is_502_and_frees_slot(client):
    FakeRoom.fail_connect = True
    r = client.post("/api/bridge/join", json={"room": ROOM, "name": "C", "model": "m"})
    assert r.status_code == 502
    assert bridge.REGISTRY.pending == [] and bridge.REGISTRY.active() == []


def test_session_only_via_bearer_header(client):
    j = _join(client)
    assert client.get("/api/bridge/status").status_code == 401
    assert client.get("/api/bridge/status", headers=_h("nope")).status_code == 404
    assert client.get("/api/bridge/status", params={"session": j["session"]}).status_code == 401
    r = client.get("/api/bridge/status", headers=_h(j["session"]))
    assert r.status_code == 200 and r.json()["connected"] is True


def test_session_token_is_stored_hashed(client):
    j = _join(client)
    assert j["session"] not in bridge.REGISTRY.sessions
    assert bridge.hash_token(j["session"]) in bridge.REGISTRY.sessions


# ----- say / send / allowlist ----------------------------------------------------

def test_say_publishes_cli_envelope(client):
    j = _join(client)
    r = client.post("/api/bridge/say", headers=_h(j["session"]), json={"text": "Hallo", "mode": "overwrite"})
    assert r.json() == {"ok": True, "seq": 1}
    topic, payload = FakeRoom.instances[-1].local_participant.published[-1]
    assert topic == "operator.say"
    assert payload["text"] == "Hallo" and payload["mode"] == "overwrite" and payload["_seq"] == 1


def test_say_rejects_bad_mode(client):
    j = _join(client)
    r = client.post("/api/bridge/say", headers=_h(j["session"]), json={"text": "x", "mode": "loud"})
    assert r.status_code == 400


@pytest.mark.parametrize("topic", ["transcript", "agent.heartbeat", "operator.notice", "operator.revise", "x"])
def test_send_allowlist_rejects(client, topic):
    j = _join(client)
    r = client.post("/api/bridge/send", headers=_h(j["session"]), json={"topic": topic, "payload": {}})
    assert r.status_code == 400


@pytest.mark.parametrize("topic", sorted(bridge.SEND_TOPICS))
def test_send_allowlist_accepts(client, topic):
    j = _join(client)
    r = client.post("/api/bridge/send", headers=_h(j["session"]), json={"topic": topic, "payload": {"text": "t"}})
    assert r.status_code == 200
    assert FakeRoom.instances[-1].local_participant.published[-1][0] == topic


def test_send_payload_size_cap(client):
    j = _join(client)
    r = client.post("/api/bridge/send", headers=_h(j["session"]),
                    json={"topic": "operator.inject", "payload": {"text": "x" * 20000}})
    assert r.status_code == 413


def test_persona_guard_server_side(client):
    FakeRoom.peers = [FakePart("voice-ai-1", kind=4),
                      FakePart("hermes-box-1", attributes={"vh.role": "agent"})]
    j = _join(client, persona="Du bist ...")
    assert any("NOT pushed" in n for n in j["notes"])
    assert not any(t == "operator.persona" for t, _ in FakeRoom.instances[-1].local_participant.published)
    r = client.post("/api/bridge/send", headers=_h(j["session"]), json={"topic": "operator.persona", "payload": {"text": "x"}})
    assert r.status_code == 409
    r = client.post("/api/bridge/send", headers=_h(j["session"]),
                    json={"topic": "operator.persona", "payload": {"text": "x"}, "force": True})
    assert r.status_code == 200


def test_persona_and_greet_pushed_when_alone(client):
    _join(client, persona="P", greet="Hallo, hier ist Claude.")
    pub = FakeRoom.instances[-1].local_participant.published
    assert pub[0] == ("operator.persona", {"text": "P"})
    assert pub[1][0] == "operator.say" and pub[1][1]["text"] == "Hallo, hier ist Claude."


def test_send_rate_limit(client, monkeypatch):
    monkeypatch.setattr(bridge, "SEND_LIMIT", 3)
    j = _join(client)
    codes = [client.post("/api/bridge/say", headers=_h(j["session"]), json={"text": f"s{i}"}).status_code
             for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]


# ----- next queue ---------------------------------------------------------------

def test_next_user_turn_revise_timeout_and_queue(client):
    j = _join(client)
    room = FakeRoom.instances[-1]
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "user", "text": "Hallo?"}, "voice-ai-1"))
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "agent", "text": "ignored"}, "voice-ai-1"))
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "user", "text": "part", "final": False}, "voice-ai-1"))
    _call(client, room.emit, "data_received", FakePacket(
        "operator.revise", {"unspoken": ["a"], "new": "b", "text": "REVISE: ..."}, "voice-ai-1"))
    h = _h(j["session"])
    r1 = client.get("/api/bridge/next", headers=h, params={"timeout": 1}).json()
    assert r1["type"] == "user" and r1["text"] == "Hallo?" and r1["pending"] == 1
    r2 = client.get("/api/bridge/next", headers=h, params={"timeout": 1}).json()
    assert r2["type"] == "revise" and r2["unspoken"] == ["a"] and r2["new"] == "b"
    r3 = client.get("/api/bridge/next", headers=h, params={"timeout": 0.1}).json()
    assert r3 == {"ok": True, "type": "timeout", "pending": 0}


def test_leave_says_goodbye_disconnects_and_next_reports_ended(client):
    j = _join(client)
    h = _h(j["session"])
    r = client.post("/api/bridge/leave", headers=h, json={"say": "Tschüss"})
    assert r.json()["type"] == "leaving"
    room = FakeRoom.instances[-1]
    assert room.disconnected
    topic, payload = room.local_participant.published[-1]
    assert topic == "operator.say" and payload["text"] == "Tschüss" and payload["mode"] == "append"
    assert client.get("/api/bridge/next", headers=h, params={"timeout": 1}).json()["type"] == "ended"
    assert client.post("/api/bridge/say", headers=h, json={"text": "x"}).status_code == 410


def test_room_end_ends_session(client):
    j = _join(client)
    room = FakeRoom.instances[-1]
    _call(client, room.emit, "disconnected", "ROOM_DELETED")
    _wait(lambda: bridge.REGISTRY.get(j["session"]).ended)
    s = bridge.REGISTRY.get(j["session"])
    assert s.end_reason == "ROOM_DELETED"
    assert client.get("/api/bridge/next", headers=_h(j["session"]), params={"timeout": 1}).json()["type"] == "ended"


# ----- guards -------------------------------------------------------------------

def test_idle_guard_announces_and_leaves(client):
    j = _join(client, idle_timeout=0.005)  # 0.3 s
    s = bridge.REGISTRY.get(j["session"])
    _wait(lambda: s.ended)
    assert s.end_reason == "idle_timeout"
    room = FakeRoom.instances[-1]
    assert room.disconnected
    assert room.local_participant.published[-1][1]["text"] == bridge.DEFAULT_IDLE_SAY


def test_running_next_counts_as_alive(client):
    j = _join(client, idle_timeout=0.005)
    r = client.get("/api/bridge/next", headers=_h(j["session"]), params={"timeout": 0.8}).json()
    assert r["type"] == "timeout"  # 0.8 s blocked > 0.3 s idle timeout, still connected
    assert not bridge.REGISTRY.get(j["session"]).ended


def test_idle_guard_off_with_zero(client):
    j = _join(client, idle_timeout=0)
    time.sleep(0.3)
    assert not bridge.REGISTRY.get(j["session"]).ended


def test_max_duration_expires(client, monkeypatch):
    monkeypatch.setenv("VH_MAX_CALL_SECONDS", "0.2")
    j = _join(client, idle_timeout=0)
    assert j["expires_in"] == 0
    s = bridge.REGISTRY.get(j["session"])
    _wait(lambda: s.ended)
    assert s.end_reason == "max_duration" and FakeRoom.instances[-1].disconnected


def test_sse_gone_leaves_after_grace(client, monkeypatch):
    monkeypatch.setattr(bridge, "SSE_GRACE_S", 0.2)
    j = _join(client, idle_timeout=0)
    s = bridge.REGISTRY.get(j["session"])
    _call(client, _sub_and_unsub, s)
    _wait(lambda: s.ended)
    assert s.end_reason == "sse_gone"


def _sub_and_unsub(s):
    q = s.add_sub()
    s.remove_sub(q)


def test_no_sse_ever_does_not_trigger_sse_guard(client, monkeypatch):
    monkeypatch.setattr(bridge, "SSE_GRACE_S", 0.05)
    j = _join(client, idle_timeout=0)
    time.sleep(0.3)
    assert not bridge.REGISTRY.get(j["session"]).ended


# ----- limits -------------------------------------------------------------------

def test_max_sessions_per_room(client, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_SESSIONS_PER_ROOM", 2)
    _join(client)
    _join(client)
    r = client.post("/api/bridge/join", json={"room": ROOM, "name": "C", "model": "m"})
    assert r.status_code == 429 and "room" in r.text


def test_max_sessions_per_ip(client, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_SESSIONS_PER_IP", 1)
    _join(client)
    r = client.post("/api/bridge/join", json={"room": "anderer-raum-XY12", "name": "C", "model": "m"})
    assert r.status_code == 429 and "address" in r.text


def test_join_rate_limit(client, monkeypatch):
    monkeypatch.setattr(bridge, "JOIN_LIMIT", 2)
    FakeRoom.fail_connect = True
    codes = [client.post("/api/bridge/join", json={"room": ROOM, "name": "C", "model": "m"}).status_code
             for _ in range(3)]
    assert codes == [502, 502, 429]


def test_ended_session_frees_capacity(client, monkeypatch):
    monkeypatch.setattr(bridge, "MAX_SESSIONS_PER_ROOM", 1)
    j = _join(client)
    client.post("/api/bridge/leave", headers=_h(j["session"]))
    _join(client)


# ----- SSE ----------------------------------------------------------------------

def _read_events(lines, n: int) -> list[tuple[str, dict]]:
    out, ev = [], None
    for line in lines:
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: "):
            out.append((ev, json.loads(line[6:])))
            if len(out) >= n:
                return out
    return out


@pytest.fixture
def live_server():
    """Real uvicorn on a free port: Starlette's TestClient buffers streaming bodies,
    so SSE is checked over a real socket (httpx streams line by line)."""
    import socket
    import threading

    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    _wait(lambda: server.started, 10)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(5)


def _emit(s, *args) -> None:
    s.loop.call_soon_threadsafe(s.room.emit, *args)


def test_sse_format_and_relay(live_server):
    import httpx

    with httpx.Client(base_url=live_server, timeout=10) as c:
        j = _join(c, idle_timeout=0)
        s = bridge.REGISTRY.get(j["session"])
        with c.stream("GET", "/api/bridge/events", headers=_h(j["session"])) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers["cache-control"] == "no-cache"
            lines = r.iter_lines()
            first = _read_events(lines, 2)
            assert [e for e, _ in first] == ["hello", "room-state"]
            hello = first[0][1]
            assert hello["room"] == ROOM and {p["identity"] for p in hello["peers"]} == {"voice-ai-1", "host-user"}
            assert next(p for p in hello["peers"] if p["identity"] == "voice-ai-1")["kind"] == 4
            _emit(s, "data_received", FakePacket("transcript", {"role": "operator", "text": "Hallo"}, "voice-ai-1"))
            _emit(s, "data_received", FakePacket("agent.heartbeat", {"ts": 1, "healthy": True}, "voice-ai-1"))
            _emit(s, "participant_connected", FakePart("gast-2"))
            _emit(s, "disconnected", "ROOM_DELETED")
            rest = _read_events(lines, 10)
    types = [e for e, _ in rest]
    assert types[0:2] == ["data", "data"]
    assert rest[0][1] == {"type": "data", "topic": "transcript",
                          "payload": {"role": "operator", "text": "Hallo"}, "sender": "voice-ai-1"}
    assert rest[1][1]["topic"] == "agent.heartbeat"
    assert "peer-joined" in types and types[-1] == "ended"
    assert rest[-1][1]["reason"] == "ROOM_DELETED"


def test_sse_ping_comment(live_server, monkeypatch):
    import httpx

    monkeypatch.setattr(bridge, "SSE_PING_S", 0.05)
    with httpx.Client(base_url=live_server, timeout=10) as c:
        j = _join(c, idle_timeout=0)
        s = bridge.REGISTRY.get(j["session"])
        seen_ping = False
        with c.stream("GET", "/api/bridge/events", headers=_h(j["session"])) as r:
            for line in r.iter_lines():
                if line == ": ping" and not seen_ping:
                    seen_ping = True
                    _emit(s, "disconnected", "ROOM_DELETED")
                if line.startswith("event: ended"):
                    break
    assert seen_ping and s.ended


def test_sse_disconnect_starts_grace_then_leaves(live_server, monkeypatch):
    import httpx

    monkeypatch.setattr(bridge, "SSE_GRACE_S", 0.3)
    with httpx.Client(base_url=live_server, timeout=10) as c:
        j = _join(c, idle_timeout=0)
        s = bridge.REGISTRY.get(j["session"])
        # own connection for the stream, like curl -N / the CLI's SSE client
        with httpx.Client(base_url=live_server, timeout=10) as c2, \
                c2.stream("GET", "/api/bridge/events", headers=_h(j["session"])) as r:
            # Keep a reference to the line iterator while asserting: a dropped
            # iter_lines() generator is finalized at once, httpcore's GeneratorExit
            # handler then closes the TCP connection, and uvicorn's http.disconnect
            # races this assert (the old flake: 0 == 1, ~4% of runs).
            lines = r.iter_lines()
            assert [e for e, _ in _read_events(lines, 2)] == ["hello", "room-state"]
            assert len(s.subs) == 1
            assert s.sse_gone_at is None and not s.ended
        # only now (stream closed by leaving the with-block) the grace period starts
        _wait(lambda: s.ended, 5)
    assert s.end_reason == "sse_gone" and s.room.disconnected


def test_nothing_secret_or_spoken_is_logged(client, caplog):
    caplog.set_level("DEBUG")
    j = _join(client, greet="GEHEIMER-GRUSS")
    client.post("/api/bridge/say", headers=_h(j["session"]), json={"text": "INHALT-XYZ"})
    client.post("/api/bridge/leave", headers=_h(j["session"]), json={"say": "BYE-ABC"})
    text = caplog.text
    assert j["session"] not in text
    for needle in ("GEHEIMER-GRUSS", "INHALT-XYZ", "BYE-ABC"):
        assert needle not in text


def test_caddy_does_not_buffer_bridge_events():
    from pathlib import Path
    cf = (Path(__file__).resolve().parents[3] / "infra" / "caddy" / "Caddyfile.tmpl").read_text()
    assert "not path /api/bridge/events" in cf
    assert "encode @enc" in cf


# ----- Paket 7: Brücke ohne Einladung nur in der Übergangsfrist -------------------

def test_join_without_invite_rejected_when_required(client, monkeypatch):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", "1")
    r = client.post("/api/bridge/join", json={"invite_url": f"https://voicehook.ai/r/{ROOM}", "name": "C", "model": "m"})
    assert r.status_code == 403
    assert r.json()["detail"] == "operator invite required"


def test_join_with_valid_invite_passes_when_required(client, monkeypatch):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", "1")
    inv = mint_invite(ROOM, 600, secret=SECRET)
    j = _join(client, invite_url=f"https://voicehook.ai/r/{ROOM}?invite={inv}")
    assert j["room"] == ROOM


def test_join_without_invite_allowed_in_transition(client, monkeypatch):
    monkeypatch.setenv("VH_REQUIRE_OPERATOR_INVITE", "0")
    assert _join(client)["room"] == ROOM


# ----- Status-Board (operator.status) über die Brücke (Oliver 02.10.) -------------

def test_send_allows_operator_status_board(client):
    j = _join(client)
    board = {"doing": "baut gerade den Fix", "open": ["Tests"], "done": ["Analyse"]}
    r = client.post("/api/bridge/send", headers=_h(j["session"]),
                    json={"topic": "operator.status", "payload": board})
    assert r.status_code == 200, r.text
    assert FakeRoom.instances[-1].local_participant.published[-1] == ("operator.status", board)
    # Positivkontrolle Allowlist: unbekanntes Topic bleibt 400
    bad = client.post("/api/bridge/send", headers=_h(j["session"]),
                      json={"topic": "operator.status_request", "payload": {}})
    assert bad.status_code == 400


def test_status_request_round_trip_reaches_next(client):
    j = _join(client)
    room = FakeRoom.instances[-1]
    _call(client, room.emit, "data_received", FakePacket(
        "operator.status_request", {"text": "was macht Claude gerade?"}, "voice-ai-1"))
    r = client.get("/api/bridge/next", headers=_h(j["session"]), params={"timeout": 1}).json()
    assert r["type"] == "status_request" and r["text"] == "was macht Claude gerade?"
    # Antwort des Agenten: Board zurück an den Worker
    ok = client.post("/api/bridge/send", headers=_h(j["session"]),
                     json={"topic": "operator.status", "payload": {"doing": "testet"}})
    assert ok.status_code == 200
    assert room.local_participant.published[-1] == ("operator.status", {"doing": "testet"})


def test_next_flags_stale_board_only_after_user_spoke(client, monkeypatch):
    j = _join(client)
    room = FakeRoom.instances[-1]
    h = _h(j["session"])
    monkeypatch.setattr(bridge, "STATUS_STALE_S", 0.0)
    r0 = client.get("/api/bridge/next", headers=h, params={"timeout": 0.05}).json()
    assert "status_stale" not in r0                    # niemand hat gesprochen
    time.sleep(0.01)
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "user", "text": "Hallo?"}, "voice-ai-1"))
    r1 = client.get("/api/bridge/next", headers=h, params={"timeout": 1}).json()
    assert r1["type"] == "user" and r1["status_stale"] is True
    client.post("/api/bridge/send", headers=h, json={"topic": "operator.status", "payload": {"doing": "x"}})
    r2 = client.get("/api/bridge/next", headers=h, params={"timeout": 0.05}).json()
    assert "status_stale" not in r2                    # frisches Board


def test_next_carries_agent_said_not_operator_echo(client):
    j = _join(client)
    room = FakeRoom.instances[-1]
    h = _h(j["session"])
    for role, text in (("agent", "Gern geschehen!"), ("operator", "Echo meines say"),
                       ("agent", "Noch was?"), ("agent", "Teil", )):
        _call(client, room.emit, "data_received", FakePacket("transcript", {"role": role, "text": text}, "voice-ai-1"))
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "agent", "text": "zwischen", "final": False}, "voice-ai-1"))
    _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "user", "text": "Danke"}, "voice-ai-1"))
    r = client.get("/api/bridge/next", headers=h, params={"timeout": 1}).json()
    assert r["type"] == "user" and r["agent_said"] == ["Gern geschehen!", "Noch was?", "Teil"]
    r2 = client.get("/api/bridge/next", headers=h, params={"timeout": 0.05}).json()
    assert "agent_said" not in r2
    for i in range(5):
        _call(client, room.emit, "data_received", FakePacket("transcript", {"role": "agent", "text": f"s{i} " + "x" * 150}, "voice-ai-1"))
    r3 = client.get("/api/bridge/next", headers=h, params={"timeout": 0.05}).json()
    assert r3["type"] == "timeout" and [t[:2] for t in r3["agent_said"]] == ["s3", "s4"]
    assert sum(map(len, r3["agent_said"])) <= 400


# ----- Lebenszeichen (operator.alive) über die Brücke (Oliver 02.10.) -------------

def test_send_allows_operator_alive(client):
    j = _join(client)
    for payload in ({"alive": True, "ts": 1790933115.4, "idle_s": 3.0}, {"alive": False, "ts": 1790933200.0}):
        r = client.post("/api/bridge/send", headers=_h(j["session"]),
                        json={"topic": "operator.alive", "payload": payload})
        assert r.status_code == 200, r.text
        assert FakeRoom.instances[-1].local_participant.published[-1] == ("operator.alive", payload)
    # Positivkontrolle Allowlist: unbekanntes Topic bleibt 400
    bad = client.post("/api/bridge/send", headers=_h(j["session"]),
                      json={"topic": "operator.alive2", "payload": {}})
    assert bad.status_code == 400
