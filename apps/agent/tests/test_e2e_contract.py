"""E2E contract test — full mouthpiece pipeline without a real LiveKit server.

The cutover gate (PLAN-v4.md): real audio→transcript→reply requires a live LK
+ Deepgram + Google + Gemini, which isn't free per CI run. This test exercises
the END-TO-END SHAPE deterministically:

    invite mint  →  /api/token (HMAC gate) → LK JWT  →  RelayAgent (StopResponse)
                 →  senior.say arrives    →  session.say(text, no-interrupt)
                 →  AutoGreeter fires on first persona, idempotent after
                 →  HeartbeatPublisher → agent.heartbeat (NOT transcript)
                 →  BackchannelWatcher → "mhm" cycling, allow_interruptions=True

Real-audio E2E lives in tests/e2e/ (gated by VOICEHOOK_E2E=1, manually triggered
via .github/workflows/e2e.yml against the staging box after first deploy).
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from livekit.agents import StopResponse

from agent.health import TOPIC_HEARTBEAT
from agent.relay import RelayAgent, build_relay_handlers, topic_dispatch
from agent.server import app
from agent.signals import AutoGreeter, BackchannelWatcher, HeartbeatPublisher
from agent.tokens import mint_invite

SECRET = "e2e-secret"


@pytest.fixture(autouse=True)
def _e2e_env(monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_API_KEY", "E2E_KEY")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "e2e_secret_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "e2e-dg")
    monkeypatch.setenv("GOOGLE_API_KEY", "e2e-gk")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")


def _pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _decode_jwt_payload(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(_pad(token.split(".")[1])))


@pytest.mark.asyncio
async def test_full_pipeline_join_to_speak():
    """Full mouthpiece contract exercised deterministically."""
    room = "e2e-room-XYZ"

    # 1) HMAC invite → /api/token → LK JWT
    invite = mint_invite(room, secret=SECRET)
    client = TestClient(app)
    r = client.post("/api/token", json={"room": room, "identity": "olli", "invite": invite})
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "wss://rtc.test"
    payload = _decode_jwt_payload(body["token"])
    assert payload["iss"] == "E2E_KEY"
    assert payload["sub"] == "olli"
    assert payload["video"]["room"] == room
    assert payload["video"]["canPublish"] is True

    # 2) /api/token without invite → 403
    r2 = client.post("/api/token", json={"room": room, "identity": "stranger", "invite": "x"})
    assert r2.status_code == 403

    # 3) RelayAgent enforces StopResponse — never auto-generates
    agent = RelayAgent(instructions="placeholder")
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed()

    # 4) senior.say → session.say(verbatim, no interruptions)
    session = MagicMock()
    session.say = MagicMock()
    session.interrupt = MagicMock()
    handlers = build_relay_handlers(session, agent)
    routes = topic_dispatch(handlers)

    say_packet = MagicMock()
    say_packet.data = b'{"text": "Hallo Olli, Claude hier."}'
    await routes["senior.say"](say_packet)
    session.say.assert_called_once_with("Hallo Olli, Claude hier.", allow_interruptions=False)

    # 5) senior.persona → live instruction injection
    persona_packet = MagicMock()
    persona_packet.data = b'{"text": "Du bist die Stimme von Olli."}'
    await routes["senior.persona"](persona_packet)
    assert agent.instructions == "Du bist die Stimme von Olli."

    # 6) AutoGreeter fires once on first persona-equivalent, idempotent after
    session2 = MagicMock()
    session2.say = MagicMock()
    greeter = AutoGreeter(session2)
    greeter.on_persona()
    greeter.on_persona()
    greeter.on_persona()
    assert session2.say.call_count == 1

    # 7) Heartbeat publishes to dedicated agent.heartbeat topic (NOT transcript)
    room_mock = MagicMock()
    room_mock.local_participant = MagicMock()
    room_mock.local_participant.publish_data = AsyncMock()
    pub = HeartbeatPublisher(room_mock, interval_s=0.02, room_name=room)
    pub.start()
    await asyncio.sleep(0.07)  # >=3 ticks
    pub.stop()
    assert room_mock.local_participant.publish_data.await_count >= 2
    first_call = room_mock.local_participant.publish_data.await_args_list[0]
    assert first_call.kwargs["topic"] == TOPIC_HEARTBEAT
    assert first_call.kwargs["topic"] != "transcript"
    p = json.loads(first_call.kwargs["payload"])
    assert p["room"] == room
    assert "probe" in p

    # 8) Backchannel: user speaks >6s without agent reply → "mhm" non-floor-taking
    session3 = MagicMock()
    session3.say = MagicMock()
    w = BackchannelWatcher(session3)
    assert w.should_fire(user_speaking_secs=6.0, agent_recently_spoke=False)
    phrase = w.fire_next()
    assert phrase == "mhm"
    _, kwargs = session3.say.call_args
    assert kwargs["allow_interruptions"] is True


def test_cutover_readiness_summary():
    """Pure documentation: what /healthz, /status, and the LK JWT shape contract."""
    # /healthz: liveness only
    c = TestClient(app)
    assert c.get("/healthz").json() == {"status": "ok"}
    # /status: functional probes (env-presence; PR-10+ promotes to round-trip)
    s = c.get("/status").json()
    assert set(s["probe"]) == {"stt", "tts", "llm"}
    assert s["healthy"] is True  # all keys set by _e2e_env
