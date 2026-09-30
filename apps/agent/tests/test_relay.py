"""operator.* relay tests — handler dispatch + StopResponse discipline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from livekit.agents import StopResponse

from agent.relay import (
    DEFAULT_PERSONA,
    TOPIC_INJECT,
    TOPIC_INTERRUPT,
    TOPIC_MODE,
    TOPIC_PERSONA,
    TOPIC_SAY,
    RelayAgent,
    build_relay_handlers,
    topic_dispatch,
)


@dataclass
class FakePacket:
    topic: str
    data: bytes


def _pkt(topic: str, payload: dict) -> FakePacket:
    return FakePacket(topic=topic, data=json.dumps(payload).encode("utf-8"))


def _fake_session() -> MagicMock:
    sess = MagicMock()
    sess.say = MagicMock()
    sess.interrupt = MagicMock()
    return sess


def _fake_agent() -> RelayAgent:
    return RelayAgent(instructions="placeholder")


@pytest.mark.asyncio
async def test_relay_agent_auto_mode_does_not_stop():
    """Default (auto) mode: on_user_turn_completed returns normally → the LLM
    answers from its persona (knowledge transfer)."""
    agent = _fake_agent()  # strict=False by default
    await agent.on_user_turn_completed()  # must NOT raise StopResponse


@pytest.mark.asyncio
async def test_relay_agent_strict_mode_raises_stop():
    """Strict mode: StopResponse → the LLM never speaks on its own."""
    agent = RelayAgent(instructions="placeholder", strict=True)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed()


@pytest.mark.asyncio
async def test_mode_handler_switches_strict():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_mode(_pkt(TOPIC_MODE, {"mode": "strict"}))
    assert agent.strict is True
    await h.on_mode(_pkt(TOPIC_MODE, {"mode": "auto"}))
    assert agent.strict is False


@pytest.mark.asyncio
async def test_say_calls_session_say_verbatim():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Hallo Welt"}))
    session.say.assert_called_once_with("Hallo Welt", allow_interruptions=True)
    # Vertrag seit Supersede: auch das erste say stoppt eine laufende Eigenantwort
    session.interrupt.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_say_publishes_transcript_when_room_given():
    """PR-12: operator.say must also publish {role:agent,text} on transcript topic."""
    from unittest.mock import AsyncMock
    session, agent = _fake_session(), _fake_agent()
    room = MagicMock()
    room.local_participant = MagicMock()
    room.local_participant.publish_data = AsyncMock()
    h = build_relay_handlers(session, agent, room=room)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Hallo Olli"}))
    await asyncio.sleep(0)  # let the asyncio.create_task fire
    await asyncio.sleep(0)
    room.local_participant.publish_data.assert_called_once()
    call = room.local_participant.publish_data.await_args
    assert call.kwargs["topic"] == "transcript"
    payload = json.loads(call.kwargs["payload"])
    assert payload == {"role": "agent", "text": "Hallo Olli"}


@pytest.mark.asyncio
async def test_say_priority_interrupt_drops_floor_then_speaks():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Stopp", "priority": "interrupt"}))
    session.interrupt.assert_called_once()
    session.say.assert_called_once_with("Stopp", allow_interruptions=True)


@pytest.mark.asyncio
async def test_say_ignores_empty_or_missing_text():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "  "}))
    await h.on_say(_pkt(TOPIC_SAY, {}))
    await h.on_say(FakePacket(topic=TOPIC_SAY, data=b"not json"))
    session.say.assert_not_called()


@pytest.mark.asyncio
async def test_persona_replaces_agent_instructions():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    new_persona = "Du bist heute Marie. Du sprichst nur Deutsch."
    await h.on_persona(_pkt(TOPIC_PERSONA, {"text": new_persona}))
    assert agent.instructions == new_persona  # update_instructions is awaited


@pytest.mark.asyncio
async def test_persona_ignores_empty():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_persona(_pkt(TOPIC_PERSONA, {"text": ""}))
    assert agent.instructions == "placeholder"  # unchanged


@pytest.mark.asyncio
async def test_interrupt_drops_current_say():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    await h.on_interrupt(_pkt(TOPIC_INTERRUPT, {}))
    session.interrupt.assert_called_once()


def test_topic_dispatch_maps_all_five_topics():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    routes = topic_dispatch(h)
    assert set(routes.keys()) == {TOPIC_SAY, TOPIC_PERSONA, TOPIC_MODE, TOPIC_INTERRUPT, TOPIC_INJECT}


def test_default_persona_includes_relay_discipline():
    assert "operator.say" in DEFAULT_PERSONA
    assert "Operator" in DEFAULT_PERSONA
    assert "erfindest NICHTS" in DEFAULT_PERSONA  # no invention


# ── Supersede: ein neues operator.say ersetzt alles noch nicht Gesprochene ──────

def _handle(done: bool = False) -> MagicMock:
    h = MagicMock()
    h.done = MagicMock(return_value=done)
    h.interrupt = MagicMock()
    return h


def _session_with_handles(*handles: MagicMock) -> MagicMock:
    sess = _fake_session()
    sess.say = MagicMock(side_effect=list(handles))
    return sess


@pytest.mark.asyncio
async def test_say_replaces_pending_say_by_default():
    first, second = _handle(), _handle()
    session = _session_with_handles(first, second)
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "alte Aussage"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "neue Entscheidung"}))
    first.interrupt.assert_called_once_with(force=True)   # veraltete Ausgabe storniert
    second.interrupt.assert_not_called()
    assert session.say.call_count == 2


@pytest.mark.asyncio
async def test_say_default_also_stops_current_speech_forced():
    # auch eine laufende Eigenantwort des Agents (auto mode) wird gestoppt
    session = _session_with_handles(_handle())
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "x"}))
    session.interrupt.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_say_append_mode_keeps_queue():
    first, second = _handle(), _handle()
    session = _session_with_handles(first, second)
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Teil 1", "mode": "append"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Teil 2", "mode": "append"}))
    first.interrupt.assert_not_called()
    session.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_say_skips_already_finished_handles():
    first, second = _handle(done=True), _handle()
    session = _session_with_handles(first, second)
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "a"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "b"}))
    first.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_clears_whole_queue():
    a, b = _handle(), _handle()
    session = _session_with_handles(a, b)
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "a", "mode": "append"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "b", "mode": "append"}))
    await h.on_interrupt(_pkt(TOPIC_INTERRUPT, {}))
    a.interrupt.assert_called_once_with(force=True)
    b.interrupt.assert_called_once_with(force=True)
    session.interrupt.assert_called_with(force=True)


@pytest.mark.asyncio
async def test_cancel_survives_runtime_error_from_livekit():
    # livekit wirft RuntimeError, wenn nichts läuft / Session nicht läuft
    bad = _handle()
    bad.interrupt.side_effect = RuntimeError("not running")
    session = _session_with_handles(bad, _handle())
    session.interrupt.side_effect = RuntimeError("session not running")
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "a"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "b"}))   # darf nicht werfen
    assert session.say.call_count == 2
