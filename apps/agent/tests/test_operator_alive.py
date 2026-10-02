"""operator.alive: Delta merkt, wenn der Agent nicht mehr zuhört (Oliver 02.10.2026).

Vorfall 02.10. 09:24-09:29 UTC (drift-calm-signal-UNVK): die CLI stand als "Claude" im
Raum, niemand bediente next/say, Delta sagte zweimal "Moment, ich schau nach." ins Leere.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import worker
from agent.alive import ALIVE_STALE_S, TOPIC_ALIVE, OperatorAlive, unreachable_sentence
from agent.relay import TOPIC_STATUS_REQUEST, build_relay_handlers, operator_persona, topic_dispatch
from agent.tests.test_agent_name_status import _Clock, _live_agent, _normal


def _alive(alive: bool = True):
    return SimpleNamespace(topic=TOPIC_ALIVE, data=json.dumps({"alive": alive, "ts": 1.0, "idle_s": 0}).encode())


# ----- Tracker ----------------------------------------------------------------------
def test_tracker_states():
    a = OperatorAlive()
    assert a.reachable(False, 0) is False                     # nicht im Raum
    assert a.reachable(True, 10_000) is True                  # legacy: nie ein Lebenszeichen
    a.on_packet({"alive": True}, 100.0)
    assert a.reachable(True, 100.0 + ALIVE_STALE_S) is True   # Grenze: noch erreichbar
    assert a.reachable(True, 100.0 + ALIVE_STALE_S + 0.1) is False
    a.on_packet({"alive": True}, 200.0)                       # frisches Lebenszeichen
    assert a.reachable(True, 201.0) is True
    a.on_packet({"alive": False}, 202.0)                      # leave/idle-timeout
    assert a.reachable(True, 202.0) is False
    a.reset()
    assert a.reachable(True, 999.0) is True                   # neuer Agent: wieder legacy


def test_fixed_sentence():
    assert unreachable_sentence("Claude") == "Claude ist gerade nicht erreichbar."
    assert unreachable_sentence(None) == "Dein Agent ist gerade nicht erreichbar."


def test_topic_routed():
    h = build_relay_handlers(MagicMock(), _normal())
    assert topic_dispatch(h)[TOPIC_ALIVE] is h.on_alive


# ----- Delta: Wartesätze werden zum festen Satz --------------------------------------
@pytest.mark.asyncio
async def test_normal_stale_then_back():
    agent, clock = _normal(), _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_alive(_alive())
    assert agent.update_instructions.await_args.args[0] == operator_persona("Claude")
    clock.t += ALIVE_STALE_S + 1
    await h.check_reach()
    t = agent.update_instructions.await_args.args[0]
    assert "'Claude ist gerade nicht erreichbar.'" in t and "statt jedes wartesatzes" in t.lower()
    n = agent.update_instructions.await_count
    await h.check_reach()                                     # kein Zustandswechsel: still
    assert agent.update_instructions.await_count == n
    await h.on_alive(_alive())                                # Positivkontrolle: zurück
    assert agent.update_instructions.await_args.args[0] == operator_persona("Claude")


@pytest.mark.asyncio
async def test_alive_false_switches_immediately():
    agent, clock = _normal(), _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_alive(_alive(False))
    assert "Claude ist gerade nicht erreichbar." in agent.update_instructions.await_args.args[0]


@pytest.mark.asyncio
async def test_legacy_cli_never_switches():
    agent, clock = _normal(), _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock)
    await h.on_agent_presence(True, "Claude")
    clock.t += 3600
    await h.check_reach()
    assert "nicht erreichbar" not in agent.update_instructions.await_args.args[0]


@pytest.mark.asyncio
async def test_live_mode_marked_turns():
    agent, clock = _live_agent(), _Clock()
    h = build_relay_handlers(MagicMock(), agent, live=True, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_alive(_alive())
    clock.t += ALIVE_STALE_S + 1
    await h.check_reach()
    last = agent.chat_ctx.items[-1].text_content
    assert last.startswith("[System]") and "Claude ist gerade nicht erreichbar." in last
    await h.on_alive(_alive())
    assert "wieder erreichbar" in agent.chat_ctx.items[-1].text_content


@pytest.mark.asyncio
async def test_status_question_answered_with_fixed_sentence():
    agent, clock, session = _normal(), _Clock(), MagicMock()
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=AsyncMock()))
    h = build_relay_handlers(session, agent, room=room, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_alive(_alive())
    clock.t += ALIVE_STALE_S + 1
    await h.check_reach()
    await h.on_user_text("Was macht Claude gerade?")
    session.say.assert_called_once_with("Claude ist gerade nicht erreichbar.", allow_interruptions=True)
    room.local_participant.publish_data.assert_not_awaited()  # keine Nachfrage ins Leere
    await h.on_alive(_alive())                                # Positivkontrolle: wieder da
    await h.on_user_text("Was macht Claude gerade?")
    assert room.local_participant.publish_data.await_args.kwargs["topic"] == TOPIC_STATUS_REQUEST


@pytest.mark.asyncio
async def test_new_agent_resets_to_legacy():
    agent, clock = _normal(), _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_alive(_alive(False))
    await h.on_agent_presence(False)
    await h.on_agent_presence(True, "Claude")
    await h.check_reach()
    assert "nicht erreichbar" not in agent.update_instructions.await_args.args[0]


def test_worker_tick_is_fast():
    assert worker.REACH_TICK_S <= 5
