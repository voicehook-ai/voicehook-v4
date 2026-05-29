"""Tests for server-side auto-greet / heartbeat / backchannel (fixes v3#61)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.health import TOPIC_HEARTBEAT
from agent.signals import (
    BACKCHANNEL_AFTER_SECONDS,
    BACKCHANNEL_CYCLE,
    DEFAULT_GREET,
    AutoGreeter,
    BackchannelWatcher,
    HeartbeatPublisher,
)


def _fake_session() -> MagicMock:
    s = MagicMock()
    s.say = MagicMock()
    return s


def _fake_room() -> MagicMock:
    r = MagicMock()
    r.local_participant = MagicMock()
    r.local_participant.publish_data = AsyncMock()
    return r


# ---------- AutoGreeter ----------------------------------------------------


def test_auto_greet_fires_once_on_first_persona():
    s = _fake_session()
    g = AutoGreeter(s)
    assert not g.fired
    g.on_persona()
    assert g.fired
    s.say.assert_called_once_with(DEFAULT_GREET, allow_interruptions=False)


def test_auto_greet_idempotent_across_personas():
    s = _fake_session()
    g = AutoGreeter(s)
    g.on_persona()
    g.on_persona()
    g.on_persona()
    assert s.say.call_count == 1


def test_auto_greet_custom_text():
    s = _fake_session()
    g = AutoGreeter(s, greet="Hallo, Marie hier.")
    g.on_persona()
    s.say.assert_called_once_with("Hallo, Marie hier.", allow_interruptions=False)


# ---------- HeartbeatPublisher --------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_publishes_to_dedicated_topic():
    room = _fake_room()
    pub = HeartbeatPublisher(room, interval_s=0.05, room_name="vh-test")
    pub.start()
    await asyncio.sleep(0.15)  # allow >=2 ticks
    pub.stop()
    # at least one call landed
    assert room.local_participant.publish_data.await_count >= 2
    call = room.local_participant.publish_data.await_args_list[0]
    assert call.kwargs["topic"] == TOPIC_HEARTBEAT
    payload = json.loads(call.kwargs["payload"].decode("utf-8"))
    assert payload["room"] == "vh-test"
    assert "probe" in payload and "healthy" in payload


@pytest.mark.asyncio
async def test_heartbeat_survives_publish_errors():
    room = _fake_room()
    room.local_participant.publish_data = AsyncMock(side_effect=RuntimeError("network down"))
    pub = HeartbeatPublisher(room, interval_s=0.05)
    pub.start()
    await asyncio.sleep(0.15)
    pub.stop()
    # loop kept running through errors → 2+ attempts
    assert room.local_participant.publish_data.await_count >= 2


@pytest.mark.asyncio
async def test_heartbeat_stop_cancels_loop():
    room = _fake_room()
    pub = HeartbeatPublisher(room, interval_s=0.05)
    task = pub.start()
    await asyncio.sleep(0.05)
    pub.stop()
    await asyncio.sleep(0.05)
    assert task.cancelled() or task.done()


# ---------- BackchannelWatcher --------------------------------------------


def test_backchannel_fires_when_user_speaks_long_and_agent_silent():
    s = _fake_session()
    w = BackchannelWatcher(s)
    assert w.should_fire(user_speaking_secs=BACKCHANNEL_AFTER_SECONDS, agent_recently_spoke=False)
    phrase = w.fire_next()
    assert phrase == "mhm"
    s.say.assert_called_once_with("mhm", allow_interruptions=True)


def test_backchannel_skips_when_agent_just_spoke():
    w = BackchannelWatcher(_fake_session())
    assert not w.should_fire(user_speaking_secs=10.0, agent_recently_spoke=True)


def test_backchannel_skips_when_user_just_started():
    w = BackchannelWatcher(_fake_session())
    assert not w.should_fire(user_speaking_secs=2.0, agent_recently_spoke=False)


def test_backchannel_cycles_phrases():
    s = _fake_session()
    w = BackchannelWatcher(s)
    fired = [w.fire_next() for _ in range(5)]
    # cycles through ["mhm","ja","ok"]
    assert fired[:3] == BACKCHANNEL_CYCLE
    assert fired[3] == "mhm"
    assert fired[4] == "ja"


def test_backchannel_uses_allow_interruptions_true():
    """Backchannel is non-interrupting — it must NOT take the floor."""
    s = _fake_session()
    BackchannelWatcher(s).fire_next()
    args, kwargs = s.say.call_args
    assert kwargs.get("allow_interruptions") is True
