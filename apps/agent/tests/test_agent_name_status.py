"""Delta nennt den Agenten beim Namen statt "Operator" + Status-Board (Oliver 02.10.2026)."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm

from agent import live
from agent.board import BOARD_BUDGET, board_block, is_status_question, normalize_board
from agent.guide import VOICEHOOK_GUIDE, agent_display_name, wait_lines
from agent.relay import (
    DEFAULT_PERSONA,
    OPERATOR_PERSONA,
    TOPIC_STATUS,
    TOPIC_STATUS_REQUEST,
    RelayAgent,
    build_relay_handlers,
    operator_persona,
)
from agent.worker import operator_agent_name


# ----- Anzeigename -------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("Claude", "Claude"),
    ("Claude Code", "Claude Code"),
    ("Claude (opus-4.6)", "Claude"),       # Modell-ID raus
    ("hermes-crown-7f3a", "hermes crown"),  # Host-Hash raus
    ("Jürgen", "Jürgen"),
    ("Sehr Langer Agentenname Mit Vielen Worten", "Sehr Langer Agentenname"),  # <= 24
    ("7f3a-91", None), ("", None), (None, None), ("x", None), ("🤖🤖", None), ("gpt-5", "gpt"),
])
def test_agent_display_name(raw, want):
    got = agent_display_name(raw)
    assert got == want
    assert got is None or len(got) <= 24


def test_wait_lines_with_name_and_fallback():
    assert wait_lines("Claude") == ("Kurzen Moment, ich frag Claude.",
                                    "Kurzen Moment, ich geb das an Claude.")
    assert wait_lines(None) == ("Kurzen Moment, ich frag deinen Agenten.",
                                "Kurzen Moment, ich geb das an deinen Agenten.")


def test_worker_takes_last_joined_agents_name():
    human = SimpleNamespace(identity="u", attributes={})
    a1 = SimpleNamespace(identity="h-1", attributes={"vh.role": "agent", "vh.name": "Hermes"})
    a2 = SimpleNamespace(identity="c-1", attributes={"vh.role": "agent", "vh.name": "Claude"})
    anon = SimpleNamespace(identity="x-1", attributes={"vh.role": "agent", "vh.name": "4711"})
    room = lambda *ps: SimpleNamespace(remote_participants={p.identity: p for p in ps})  # noqa: E731
    assert operator_agent_name(room(human, a1, a2)) == "Claude"
    assert operator_agent_name(room(a2, a1)) == "Hermes"
    assert operator_agent_name(room(human, anon)) is None
    assert operator_agent_name(room(human)) is None


# ----- kein "Operator" zum Nutzer (grep über alle Prompt-Texte) -----------------------
_OPERATOR = re.compile(r"operator", re.IGNORECASE)
_MARKER = "[Operator] "  # rein technische Turn-Markierung im Live-Modus, nie gesprochen


def _user_facing_texts():
    board = {"doing": "baut gerade den Fix", "open": ["Tests"], "done": ["Analyse"]}
    yield "guide", VOICEHOOK_GUIDE
    for n in (None, "Claude"):
        yield f"operator_persona({n})", operator_persona(n)
        yield f"live_core({n})", live.live_core_instructions(n)
        yield f"joined({n})", live.live_agent_joined_user(n).removeprefix(_MARKER)
        yield f"status({n})", live.live_status_user(board, n).removeprefix(_MARKER)
        yield f"status_clear({n})", live.live_status_user(None, n).removeprefix(_MARKER)
        yield f"board({n})", board_block(board, n or "dein Agent")
        yield f"wait({n})", " ".join(wait_lines(n))
    yield "DEFAULT_PERSONA", DEFAULT_PERSONA
    yield "OPERATOR_PERSONA", OPERATOR_PERSONA
    yield "LIVE_BASE", live.LIVE_BASE_INSTRUCTIONS
    yield "LEFT", live.LIVE_AGENT_LEFT_USER.removeprefix(_MARKER)
    yield "PERSONA", live.LIVE_PERSONA_USER.removeprefix(_MARKER)
    yield "SAY", live.LIVE_SAY_USER.removeprefix(_MARKER)
    yield "SAY_VERBATIM", live.LIVE_SAY_VERBATIM_USER.removeprefix(_MARKER)


@pytest.mark.parametrize("label,text", list(_user_facing_texts()))
def test_no_operator_word_in_prompts(label, text):
    assert not _OPERATOR.search(text), (label, text[max(0, _OPERATOR.search(text).start() - 40):][:90])


def test_operator_grep_positive_control():
    # Positivkontrolle: der Stand auf main ("ich geb das an den Operator") wäre aufgefallen
    assert _OPERATOR.search("Moment, ich geb das an den Operator")
    assert _OPERATOR.search("wartest auf operator.say")


# ----- Join: Name in den Prompts, live umgeschaltet ----------------------------------
def _normal():
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    agent.update_instructions = AsyncMock()
    return agent


def _live_agent():
    agent = MagicMock()
    agent.chat_ctx = llm.ChatContext.empty()

    async def _upd(ctx):
        agent.chat_ctx = ctx

    agent.update_chat_ctx = AsyncMock(side_effect=_upd)
    agent.update_instructions = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_normal_join_names_agent_and_switches_on_new_agent():
    agent = _normal()
    h = build_relay_handlers(MagicMock(), agent)
    await h.on_agent_presence(True, "Claude")
    t = agent.update_instructions.await_args.args[0]
    assert "Kurzen Moment, ich frag Claude." in t and "ich geb das an Claude." in t
    assert "Operator" not in t and "deinen Agenten" not in t
    await h.on_agent_presence(True, "Claude")            # gleiches Event: idempotent
    assert agent.update_instructions.await_count == 1
    await h.on_agent_presence(True, "Hermes")            # neuer zuletzt beigetretener Agent
    assert "ich frag Hermes." in agent.update_instructions.await_args.args[0]
    await h.on_agent_presence(False)
    assert agent.update_instructions.await_args.args[0] == DEFAULT_PERSONA


@pytest.mark.asyncio
async def test_normal_join_without_name_falls_back():
    agent = _normal()
    h = build_relay_handlers(MagicMock(), agent)
    await h.on_agent_presence(True, None)
    t = agent.update_instructions.await_args.args[0]
    assert t == OPERATOR_PERSONA and "Kurzen Moment, ich frag deinen Agenten." in t


@pytest.mark.asyncio
async def test_live_join_names_agent_in_user_turn():
    agent = _live_agent()
    h = build_relay_handlers(MagicMock(), agent, live=True)
    await h.on_agent_presence(True, "Claude")
    turn = agent.chat_ctx.items[-1].text_content
    assert turn.startswith("[Operator] Claude ist jetzt im Raum.")
    assert "Kurzen Moment, ich frag Claude." in turn
    agent.update_instructions.assert_not_awaited()


# ----- Board: Budget, Ersetzen, Rate-Limit, Nachfrage ---------------------------------
def test_board_budget_cuts_done_first_then_open():
    long = "x" * 100
    b = normalize_board({"doing": "baut", "open": [f"o{i} {long}" for i in range(4)],
                         "done": [f"d{i} {long}" for i in range(4)]})
    size = len(b["doing"]) + sum(map(len, b["open"])) + sum(map(len, b["done"]))
    assert size <= BOARD_BUDGET
    assert len(b["open"]) == 4 and len(b["done"]) == 1 and b["done"][0].startswith("d3")
    b2 = normalize_board({"doing": "baut", "open": [f"o{i} {long}" for i in range(8)], "done": ["a"]})
    assert b2["done"] == [] and [o[:2] for o in b2["open"]] == ["o0", "o1", "o2", "o3", "o4"]
    # Positivkontrolle: ohne Überlauf wird nichts gekappt
    assert normalize_board({"doing": "a", "open": ["b"], "done": ["c"]}) == {
        "doing": "a", "open": ["b"], "done": ["c"]}


@pytest.mark.parametrize("payload", [{}, {"doing": ""}, {"doing": "fertig"}, {"text": "Fertig."},
                                     {"doing": "  ", "open": [], "done": []}, "kein dict"])
def test_board_clears(payload):
    assert normalize_board(payload) is None


def _pkt(payload):
    return SimpleNamespace(topic=TOPIC_STATUS, data=json.dumps(payload).encode())


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.mark.asyncio
async def test_normal_status_replaces_never_appends():
    agent = _normal()
    clock = _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock)
    await h.on_agent_presence(True, "Claude")
    lengths = []
    for i in range(50):
        clock.t += 6
        await h.on_status(_pkt({"doing": f"baut gerade Schritt {i:02d}", "open": ["Tests"]}))
        lengths.append(len(agent.update_instructions.await_args.args[0]))
    t = agent.update_instructions.await_args.args[0]
    assert len(set(lengths)) == 1                                 # Länge konstant
    assert t.count("Aktueller Stand von Claude") == 1 and "Schritt 49" in t and "Schritt 48" not in t
    assert "Kurz Moment, Claude baut gerade Schritt 49." in t     # Name + Status
    clock.t += 6
    await h.on_status(_pkt({"doing": "fertig"}))
    assert agent.update_instructions.await_args.args[0] == operator_persona("Claude")


@pytest.mark.asyncio
async def test_live_status_replaces_turn_in_local_context():
    agent = _live_agent()
    clock = _Clock()
    h = build_relay_handlers(MagicMock(), agent, live=True, clock=clock)
    await h.on_agent_presence(True, "Claude")
    counts = []
    for i in range(50):
        clock.t += 6
        await h.on_status(_pkt({"doing": f"baut Schritt {i}"}))
        counts.append(len(agent.chat_ctx.items))
    assert len(set(counts)) == 1                                  # Kontext wächst nicht
    status = [i for i in agent.chat_ctx.items if i.id.startswith("vh-status-")]
    assert len(status) == 1 and "Claude baut Schritt 49" in status[0].text_content
    agent.update_instructions.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_rate_limit_last_one_wins():
    agent = _normal()
    clock = _Clock()
    h = build_relay_handlers(MagicMock(), agent, clock=clock, status_interval_s=0.05)
    await h.on_agent_presence(True, "Claude")
    await h.on_status(_pkt({"doing": "eins"}))                   # sofort
    assert agent.update_instructions.await_count == 2
    for w in ("zwei", "drei", "vier"):                           # innerhalb des Fensters
        await h.on_status(_pkt({"doing": w}))
    assert agent.update_instructions.await_count == 2            # noch nichts angewandt
    clock.t += 1
    await asyncio.sleep(0.1)
    assert agent.update_instructions.await_count == 3            # genau ein weiteres Update
    t = agent.update_instructions.await_args.args[0]
    assert "Claude vier" in t and "zwei" not in t and "drei" not in t


@pytest.mark.parametrize("q", ["Was macht Claude gerade?", "wie weit bist du?", "Wie ist der Stand?",
                               "was macht der agent eigentlich", "Status?"])
def test_status_question_detected(q):
    assert is_status_question(q)


@pytest.mark.parametrize("q", ["Was kostet das?", "Mach das Licht an", "wie geht es dir",
                               "Stand der Technik ist gut"])
def test_status_question_not_detected(q):
    assert not is_status_question(q)


@pytest.mark.asyncio
async def test_status_request_round_trip_speaks_fresh_board():
    agent = _normal()
    session = MagicMock()
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=AsyncMock()))
    clock = _Clock()
    h = build_relay_handlers(session, agent, room=room, clock=clock)
    await h.on_user_text("Was macht Claude gerade?")             # kein Agent da: nichts
    room.local_participant.publish_data.assert_not_awaited()
    await h.on_agent_presence(True, "Claude")
    await h.on_user_text("Was kostet das?")                      # keine Nachfrage
    room.local_participant.publish_data.assert_not_awaited()
    await h.on_user_text("Was macht Claude gerade?")
    kw = room.local_participant.publish_data.await_args.kwargs
    assert kw["topic"] == TOPIC_STATUS_REQUEST and kw["reliable"] is True
    await h.on_user_text("wie weit bist du?")                    # offene Nachfrage: kein Spam
    assert room.local_participant.publish_data.await_count == 1
    clock.t += 3
    await h.on_status(_pkt({"doing": "baut gerade den Fix"}))
    session.say.assert_called_once_with("Claude baut gerade den Fix.", allow_interruptions=True)
    clock.t += 6
    await h.on_status(_pkt({"doing": "testet"}))                 # ohne Nachfrage: still
    assert session.say.call_count == 1


@pytest.mark.asyncio
async def test_status_answer_window_expires():
    agent = _normal()
    session = MagicMock()
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=AsyncMock()))
    clock = _Clock()
    h = build_relay_handlers(session, agent, room=room, clock=clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_user_text("Was macht Claude gerade?")
    clock.t += 30                                                # zu spät
    await h.on_status(_pkt({"doing": "baut"}))
    session.say.assert_not_called()


# ----- Weitergeben: kurz, variiert, ohne Rechtfertigung (Oliver 02.10.) --------------
@pytest.mark.parametrize("label,text", [
    ("normal", DEFAULT_PERSONA), ("neutral", OPERATOR_PERSONA),
    ("neutral_claude", operator_persona("Claude")), ("live", live.LIVE_BASE_INSTRUCTIONS),
    ("live_joined_claude", live.live_agent_joined_user("Claude")),
])
def test_prompts_forbid_excuses_and_vary_handoff(label, text):
    from agent.guide import NO_EXCUSE_RULE

    assert NO_EXCUSE_RULE in text, label
    assert "Gib nie eine Begründung oder Erklärung, warum du etwas nicht weißt" in text
    name = "Claude" if "claude" in label else "deinen Agenten"
    assert f"Kurzen Moment, ich frag {name}." in text
    assert "nicht immer denselben" in text and text.count(" / ") >= 3


def test_handoff_variants_named():
    from agent.guide import handoff_variants

    v = handoff_variants("Claude")
    assert v == ("Kurzen Moment, ich frag Claude.", "Gute Frage, Claude schaut kurz.",
                 "Moment, Claude ist dran.", "Kurzen Moment, ich geb das an Claude.")
    assert all("Claude" in x and "Operator" not in x for x in v)
    assert "dein Agent ist dran." in " ".join(handoff_variants(None))
