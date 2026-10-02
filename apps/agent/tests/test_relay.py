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
    # nichts Eigenes offen: einreihen, laufende Eigenantwort NICHT abbrechen
    session.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_say_does_not_publish_transcript_itself():
    """Transkript kommt vom Worker (conversation_item_added, tatsächlich Gesprochenes),
    damit Eigenantworten erscheinen und Operator-Sätze nicht doppelt."""
    from unittest.mock import AsyncMock
    session, agent = _fake_session(), _fake_agent()
    room = MagicMock()
    room.local_participant = MagicMock()
    room.local_participant.publish_data = AsyncMock()
    h = build_relay_handlers(session, agent, room=room)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Hallo Olli"}))
    await asyncio.sleep(0)
    room.local_participant.publish_data.assert_not_called()
    session.say.assert_called_once_with("Hallo Olli", allow_interruptions=True)


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


def test_topic_dispatch_maps_all_seven_topics():
    session, agent = _fake_session(), _fake_agent()
    h = build_relay_handlers(session, agent)
    routes = topic_dispatch(h)
    assert set(routes.keys()) == {TOPIC_SAY, TOPIC_PERSONA, TOPIC_MODE, TOPIC_INTERRUPT, TOPIC_INJECT,
                                  "operator.status", "operator.alive"}


def test_default_persona_includes_relay_discipline():
    # Oliver 02.10.: zum Nutzer nie "Operator", Wartesatz mit Agent statt Operator
    assert "Operator" not in DEFAULT_PERSONA and "operator.say" not in DEFAULT_PERSONA
    assert "Kurzen Moment, ich geb das an deinen Agenten." in DEFAULT_PERSONA
    assert "erfindest NICHTS" in DEFAULT_PERSONA  # no invention


# ── Revise: ungesprochene Aussagen gehen zurück ans Brain, das zusammenfasst ──

from unittest.mock import AsyncMock  # noqa: E402

from agent.relay import TOPIC_REVISE, unspoken_rest  # noqa: E402


class _Msg:
    def __init__(self, text: str) -> None:
        self.text_content = text


def _handle(done: bool = False, spoken: str | None = None) -> MagicMock:
    """spoken=None: noch nichts gesprochen; sonst der tatsächlich gesprochene Teil."""
    h = MagicMock()
    h.done = MagicMock(return_value=done)
    h.interrupted = False
    h.chat_items = [] if spoken is None else [_Msg(spoken)]
    h.interrupt = MagicMock()
    h.wait_for_playout = AsyncMock()
    return h


def _session_with_handles(*handles: MagicMock) -> MagicMock:
    sess = _fake_session()
    sess.say = MagicMock(side_effect=list(handles))
    return sess


def _room() -> MagicMock:
    room = MagicMock()
    room.local_participant.publish_data = AsyncMock()
    return room


def _published(room: MagicMock, topic: str) -> list[dict]:
    out = []
    for call in room.local_participant.publish_data.await_args_list:
        if call.kwargs.get("topic") == topic:
            out.append(json.loads(call.kwargs["payload"].decode()))
    return out


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_unspoken_rest_cuts_spoken_prefix():
    assert unspoken_rest("Hallo Welt, das ist falsch.", "Hallo Welt,") == "das ist falsch."
    assert unspoken_rest("Hallo Welt", "") == "Hallo Welt"
    assert unspoken_rest("Hallo Welt", "Hallo Welt") == ""
    # Transkript weicht in Interpunktion ab -> wortweise
    assert unspoken_rest("Eins zwei drei vier", "eins zwei") == "drei vier"


@pytest.mark.asyncio
async def test_say_speaks_immediately_when_nothing_pending():
    room = _room()
    session = _session_with_handles(_handle())
    h = build_relay_handlers(session, _fake_agent(), room=room)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Hallo", "seq": 1}))
    session.say.assert_called_once_with("Hallo", allow_interruptions=True)
    assert _published(room, TOPIC_REVISE) == []


@pytest.mark.asyncio
async def test_say_with_pending_holds_and_asks_brain_to_merge():
    room = _room()
    old = _handle(spoken="Wir nehmen")          # angefangen, Rest ungesprochen
    session = _session_with_handles(old, _handle())
    h = build_relay_handlers(session, _fake_agent(), room=room, hold_s=30)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Wir nehmen Variante A.", "seq": 1}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Doch Variante B.", "seq": 2}))
    await _drain()
    old.interrupt.assert_called_once_with(force=True)
    assert session.say.call_count == 1                     # neue Aussage zurückgehalten
    rev = _published(room, TOPIC_REVISE)
    assert len(rev) == 1
    assert rev[0]["unspoken"] == ["Variante A."]
    assert rev[0]["new"] == "Doch Variante B."
    assert "overwrite" in rev[0]["text"] and "Variante A." in rev[0]["text"]


@pytest.mark.asyncio
async def test_overwrite_speaks_merged_and_cancels_hold():
    room = _room()
    session = _session_with_handles(_handle(spoken=None), _handle())
    h = build_relay_handlers(session, _fake_agent(), room=room, hold_s=0.05)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "A", "seq": 1}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "B", "seq": 2}))       # -> revise, hold
    await h.on_say(_pkt(TOPIC_SAY, {"text": "A und B zusammen", "mode": "overwrite"}))
    await asyncio.sleep(0.1)                                        # Hold-Frist verstrichen
    spoken = [c.args[0] for c in session.say.call_args_list]
    assert spoken == ["A", "A und B zusammen"]                      # B nicht nachträglich


@pytest.mark.asyncio
async def test_hold_timeout_speaks_new_text_to_avoid_silence():
    room = _room()
    session = _session_with_handles(_handle(), _handle())
    h = build_relay_handlers(session, _fake_agent(), room=room, hold_s=0.05)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "A", "seq": 1}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "B", "seq": 2}))
    await asyncio.sleep(0.15)
    assert [c.args[0] for c in session.say.call_args_list] == ["A", "B"]


@pytest.mark.asyncio
async def test_finished_says_are_not_reported_as_unspoken():
    room = _room()
    done = _handle(done=True, spoken="A")
    session = _session_with_handles(done, _handle())
    h = build_relay_handlers(session, _fake_agent(), room=room)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "A", "seq": 1}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "B", "seq": 2}))
    assert session.say.call_count == 2                      # nichts offen -> sofort
    assert _published(room, TOPIC_REVISE) == []
    done.interrupt.assert_not_called()


@pytest.mark.asyncio
async def test_append_mode_keeps_queue():
    first, second = _handle(), _handle()
    session = _session_with_handles(first, second)
    h = build_relay_handlers(session, _fake_agent(), room=_room())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Teil 1", "mode": "append"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Teil 2", "mode": "append"}))
    first.interrupt.assert_not_called()
    assert session.say.call_count == 2


@pytest.mark.asyncio
async def test_interrupt_stops_all_and_reports_unspoken():
    room = _room()
    a = _handle(spoken=None)
    session = _session_with_handles(a)
    h = build_relay_handlers(session, _fake_agent(), room=room)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "falsche Info", "seq": 1}))
    await h.on_interrupt(_pkt(TOPIC_INTERRUPT, {}))
    await _drain()
    a.interrupt.assert_called_once_with(force=True)
    session.interrupt.assert_called_with(force=True)
    rev = _published(room, TOPIC_REVISE)
    assert rev and rev[-1]["unspoken"] == ["falsche Info"] and rev[-1]["new"] == ""


@pytest.mark.asyncio
async def test_cancel_survives_runtime_error_from_livekit():
    bad = _handle()
    bad.interrupt.side_effect = RuntimeError("not running")
    session = _session_with_handles(bad, _handle())
    session.interrupt.side_effect = RuntimeError("session not running")
    h = build_relay_handlers(session, _fake_agent(), room=_room(), hold_s=0.01)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "a"}))
    await h.on_say(_pkt(TOPIC_SAY, {"text": "b", "mode": "overwrite"}))   # darf nicht werfen
    assert session.say.call_count == 2


# ── Transkript-Farben: Operator (rot) vs. Agent selbst (blau) ────────────────
@pytest.mark.asyncio
async def test_is_operator_speech_by_handle_and_text():
    h1 = _handle()
    session = _session_with_handles(h1)
    h = build_relay_handlers(session, _fake_agent())
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Operator sagt etwas Wichtiges."}))
    assert h.is_operator_speech(h1, "egal") is True                      # über Handle
    assert h.is_operator_speech(None, "Operator sagt etwas") is True      # abgebrochen: Anfang
    assert h.is_operator_speech(None, "Delta antwortet selbst.") is False
    assert h.is_operator_speech(object(), "") is False


@pytest.mark.asyncio
async def test_is_operator_speech_live_only_by_handle():
    session = MagicMock()
    hd = MagicMock()
    session.generate_reply = MagicMock(return_value=hd)
    h = build_relay_handlers(session, _fake_agent(), live=True)
    await h.on_say(_pkt(TOPIC_SAY, {"text": "Termin Dienstag"}))
    assert h.is_operator_speech(hd, "Der Termin ist am Dienstag.") is True
    assert h.is_operator_speech(None, "Termin Dienstag") is False        # live: Text zählt nicht
