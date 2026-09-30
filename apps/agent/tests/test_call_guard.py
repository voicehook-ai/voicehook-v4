"""Cost caps for the voice-ai session (CallGuard).

Fakes only — no LiveKit server. Caps are set to tiny values so every test is
bounded to well under a second of wall-clock.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace

import pytest
from livekit import rtc
from livekit.agents import CloseReason

import agent.worker as worker
from agent.worker import CallGuard, is_human


class FakeEmitter:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event, cb=None):  # noqa: ANN001
        self.handlers.setdefault(event, []).append(cb)
        return cb

    def emit(self, event, *args):  # noqa: ANN001
        for cb in self.handlers.get(event, []):
            cb(*args)


class FakeRoom(FakeEmitter):
    def __init__(self, name: str = "test-room") -> None:
        super().__init__()
        self.name = name
        self.remote_participants: dict[str, SimpleNamespace] = {}

    def join(self, identity: str, kind: int = rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD):
        p = SimpleNamespace(identity=identity, kind=kind)
        self.remote_participants[identity] = p
        self.emit("participant_connected", p)

    def leave(self, identity: str) -> None:
        p = self.remote_participants.pop(identity)
        self.emit("participant_disconnected", p)


class FakeHandle:
    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session

    async def wait_for_playout(self) -> None:
        await asyncio.sleep(0)
        self._session.played.append(self._session.said[-1])


class FakeSession(FakeEmitter):
    def __init__(self) -> None:
        super().__init__()
        self.said: list[str] = []
        self.played: list[str] = []
        self.closed = 0

    def say(self, text: str, **_kw):  # noqa: ANN003
        self.said.append(text)
        return FakeHandle(self)

    async def aclose(self) -> None:
        self.closed += 1
        self.emit("close", SimpleNamespace(reason=CloseReason.USER_INITIATED))


class FakeCtx:
    def __init__(self, room: FakeRoom) -> None:
        self.room = room
        self.job = SimpleNamespace(id="AJ_test")
        self.deleted: list[str] = []
        self.shutdowns: list[str] = []

    def delete_room(self, room_name: str | None = None):
        self.deleted.append(room_name)
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut

    def shutdown(self, reason: str = "") -> None:
        self.shutdowns.append(reason)


def _guard(room: FakeRoom, *, max_s: float = 5.0, idle_s: float = 5.0):
    ctx, session = FakeCtx(room), FakeSession()
    guard = CallGuard(ctx, session, max_seconds=max_s, idle_seconds=idle_s)
    return guard, ctx, session


async def _settle(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _call_end_lines(caplog) -> list[dict]:  # noqa: ANN001
    return [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("call_end ")
    ]


# -- is_human ---------------------------------------------------------------
@pytest.mark.parametrize(
    "identity,kind,expected",
    [
        ("gast-ab12", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, True),
        ("host-x9", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, True),
        ("voice-ai", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, False),
        ("agent-AJ_abc", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, False),
        ("claude-senior1", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, False),
        ("hermes-1", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, False),
        ("whoever", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT, False),
        ("somebody", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, True),
    ],
)
def test_is_human(identity, kind, expected):  # noqa: ANN001
    assert is_human(SimpleNamespace(identity=identity, kind=kind)) is expected


# -- env parsing: caps can never be disabled --------------------------------
def test_max_call_seconds_default(monkeypatch):
    monkeypatch.delenv("VH_MAX_CALL_SECONDS", raising=False)
    assert worker.max_call_seconds() == 3600.0


def test_max_call_seconds_env_override(monkeypatch):
    monkeypatch.setenv("VH_MAX_CALL_SECONDS", "900")
    assert worker.max_call_seconds() == 900.0


@pytest.mark.parametrize("raw", ["0", "-5", "abc", "", "nan-ish"])
def test_invalid_or_nonpositive_env_falls_back_to_default(monkeypatch, raw):  # noqa: ANN001
    monkeypatch.setenv("VH_MAX_CALL_SECONDS", raw)
    monkeypatch.setenv("VH_IDLE_NO_HUMAN_SECONDS", raw)
    assert worker.max_call_seconds() == 3600.0
    assert worker.idle_no_human_seconds() == 60.0


def test_idle_default_is_60s(monkeypatch):
    monkeypatch.delenv("VH_IDLE_NO_HUMAN_SECONDS", raising=False)
    assert worker.idle_no_human_seconds() == 60.0


# -- hard max duration -------------------------------------------------------
async def test_max_duration_announces_closes_deletes_and_leaves(caplog):
    caplog.set_level(logging.INFO, logger="voicehook.worker")
    room = FakeRoom()
    room.join("gast-1")
    guard, ctx, session = _guard(room, max_s=0.05, idle_s=5.0)
    guard.start()
    await _settle(0.2)

    assert guard.end_reason == "max_duration"
    assert session.said == [worker.MAX_CALL_ANNOUNCEMENT]
    assert session.played == [worker.MAX_CALL_ANNOUNCEMENT]  # announced BEFORE teardown
    assert session.closed == 1
    assert ctx.deleted == ["test-room"]
    assert ctx.shutdowns == ["call_guard:max_duration"]
    lines = _call_end_lines(caplog)
    assert len(lines) == 1  # the session "close" echo must not log a 2nd end
    assert lines[0]["reason"] == "max_duration"
    assert lines[0]["room"] == "test-room"
    assert lines[0]["duration_s"] >= 0.0


async def test_max_duration_fires_even_while_human_present():
    room = FakeRoom()
    room.join("gast-1")
    guard, ctx, _ = _guard(room, max_s=0.05)
    guard.start()
    await _settle(0.2)
    assert guard.end_reason == "max_duration"
    assert ctx.shutdowns


async def test_announcement_is_short_enough_for_tts():
    assert len(worker.MAX_CALL_ANNOUNCEMENT) <= 60


# -- idle cap: no human ------------------------------------------------------
async def test_idle_ends_when_no_human_from_start():
    room = FakeRoom()
    room.join("claude-senior")  # bot only
    guard, ctx, session = _guard(room, max_s=5.0, idle_s=0.05)
    guard.start()
    await _settle(0.2)
    assert guard.end_reason == "idle_no_human"
    assert session.said == []  # no announcement for an empty room
    assert ctx.deleted == []  # Raum bleibt: Operator bleibt drin, Agent kommt per Re-Dispatch zurück
    assert ctx.shutdowns == ["call_guard:idle_no_human"]


async def test_idle_ends_after_last_human_leaves(caplog):
    caplog.set_level(logging.INFO, logger="voicehook.worker")
    room = FakeRoom()
    room.join("gast-1")
    room.join("hermes-bot")
    guard, ctx, _ = _guard(room, max_s=5.0, idle_s=0.05)
    guard.start()
    await _settle(0.1)
    assert guard.end_reason is None  # human present → no idle end
    room.leave("gast-1")  # senior CLI stays → used to keep paid TTS alive
    await _settle(0.2)
    assert guard.end_reason == "idle_no_human"
    assert ctx.deleted == []  # Raum bleibt: Operator bleibt drin, Agent kommt per Re-Dispatch zurück
    assert [ln["reason"] for ln in _call_end_lines(caplog)] == ["idle_no_human"]


async def test_idle_timer_cancelled_when_human_rejoins():
    room = FakeRoom()
    room.join("gast-1")
    guard, ctx, _ = _guard(room, max_s=5.0, idle_s=0.1)
    guard.start()
    room.leave("gast-1")
    await _settle(0.03)
    room.join("gast-1")  # F5 / reconnect within the grace window
    await _settle(0.2)
    assert guard.end_reason is None
    assert ctx.shutdowns == []
    await guard.end("test_cleanup", delete_room=False)


# -- any other session close → voice-ai leaves -------------------------------
async def test_session_close_by_lib_leaves_and_deletes_when_empty(caplog):
    caplog.set_level(logging.INFO, logger="voicehook.worker")
    room = FakeRoom()
    room.join("gast-1")
    guard, ctx, session = _guard(room)
    guard.start()
    room.leave("gast-1")
    session.emit("close", SimpleNamespace(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await _settle(0.05)
    assert guard.end_reason == "session_close:participant_disconnected"
    assert session.closed == 0  # already closed by the lib, don't re-close
    assert ctx.deleted == []  # Raum bleibt: Operator bleibt drin, Agent kommt per Re-Dispatch zurück
    assert ctx.shutdowns == ["call_guard:session_close:participant_disconnected"]
    assert len(_call_end_lines(caplog)) == 1


async def test_session_close_keeps_room_when_humans_remain():
    room = FakeRoom()
    room.join("gast-1")
    room.join("host-2")
    guard, ctx, session = _guard(room)
    guard.start()
    room.leave("gast-1")
    session.emit("close", SimpleNamespace(reason=CloseReason.PARTICIPANT_DISCONNECTED))
    await _settle(0.05)
    assert ctx.deleted == []  # don't kick the remaining person
    assert ctx.shutdowns  # but voice-ai (the paid part) leaves


# -- teardown is bounded and always reaches shutdown -------------------------
async def test_teardown_steps_are_bounded_and_shutdown_always_runs(monkeypatch):
    monkeypatch.setattr(worker, "TEARDOWN_STEP_TIMEOUT", 0.05)
    room = FakeRoom()
    guard, ctx, session = _guard(room, max_s=0.01, idle_s=5.0)

    class HangingHandle:
        async def wait_for_playout(self) -> None:
            await asyncio.sleep(3600)

    session.say = lambda *_a, **_k: HangingHandle()

    async def boom() -> None:
        raise RuntimeError("aclose failed")

    session.aclose = boom
    guard.start()
    await _settle(0.4)
    assert guard.end_reason == "max_duration"
    assert ctx.deleted == ["test-room"]
    assert ctx.shutdowns == ["call_guard:max_duration"]


async def test_end_is_idempotent():
    room = FakeRoom()
    room.join("gast-1")
    guard, ctx, _ = _guard(room)
    guard.start()
    await guard.end("first", delete_room=True)
    await guard.end("second", delete_room=True)
    assert guard.end_reason == "first"
    assert ctx.shutdowns == ["call_guard:first"]


# -- wiring guard: entrypoint arms the caps ----------------------------------
def test_entrypoint_arms_call_guard_and_explicit_room_options():
    src = inspect.getsource(worker.entrypoint)
    assert "CallGuard(" in src
    assert "max_seconds=max_call_seconds()" in src
    assert "idle_seconds=idle_no_human_seconds()" in src
    assert "close_on_disconnect=True" in src
    assert src.index("CallGuard(") < src.index("await session.start(")


# -- Live-Testworker: kein TTS, Ansage über generate_reply --------------------
async def test_live_announce_uses_generate_reply_not_say():
    room = FakeRoom()
    room.join("gast-1")
    ctx, session = FakeCtx(room), FakeSession()
    instr: list[str] = []

    def generate_reply(*, instructions: str, **_kw):  # noqa: ANN003
        instr.append(instructions)
        return FakeHandle(session)

    session.generate_reply = generate_reply
    guard = CallGuard(ctx, session, max_seconds=0.05, idle_seconds=5.0, live=True)
    guard.start()
    await _settle(0.2)
    assert guard.end_reason == "max_duration"
    assert session.said == []                                   # say() hätte RuntimeError geworfen
    assert instr and worker.MAX_CALL_ANNOUNCEMENT in instr[0]
    assert ctx.shutdowns == ["call_guard:max_duration"]
