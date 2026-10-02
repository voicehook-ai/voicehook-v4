"""Delta antwortet aus Claudes bisherigen Aussagen, Aktivitäts-Feed (operator.activity)
und Board-Feld faq (Oliver 02.10.2026, live im Call freigegeben).

Positivkontrolle: jeder Test hier ist auf origin/main (2c88ec4) rot (Regeltext alt, kein
activity.py, kein faq, Zusammenfassung nennt Claudes Sätze "Delta")."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import llm

from agent import live
from agent.activity import (
    ACTIVITY_BUDGET,
    TOPIC_ACTIVITY,
    activity_block,
    normalize_activity,
    scrub,
)
from agent.board import board_block, normalize_board
from agent.bridge import SEND_TOPICS
from agent.core import core_live, core_normal
from agent.history import HistoryKeeper
from agent.relay import (
    DEFAULT_PERSONA,
    RelayAgent,
    build_relay_handlers,
    operator_persona,
    topic_dispatch,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _pkt(payload, topic=TOPIC_ACTIVITY):
    return SimpleNamespace(topic=topic, data=json.dumps(payload).encode())


def _normal(clock=None, **kw):
    agent = RelayAgent(instructions=DEFAULT_PERSONA, speakers=None)
    agent.update_instructions = AsyncMock()
    return agent, build_relay_handlers(MagicMock(), agent, clock=clock or _Clock(), **kw)


def _live_agent():
    agent = MagicMock()
    agent.chat_ctx = llm.ChatContext.empty()

    async def _upd(ctx):
        agent.chat_ctx = ctx

    agent.update_chat_ctx = AsyncMock(side_effect=_upd)
    agent.update_instructions = AsyncMock()
    return agent


def _last(agent) -> str:
    return agent.update_instructions.await_args.args[0]


# ===== 1 Kern: Claudes Aussagen im Call sind eine Quelle ===============================
def test_normal_core_names_agent_statements_as_source():
    t = core_normal("Claude")
    assert ("nennst du nur, wenn es unten im Wissen oder im Status steht oder Claude es in "
            "diesem Call schon gesagt hat.") in t
    assert ("3. Steht etwas im Status, im Wissen oder in dem, was Claude in diesem Call gesagt "
            "hat ([Claude]), beantwortest du Fragen dazu frei und inhaltlich") in t
    assert "Steht es nirgends davon, sagst du genau einen kurzen Wartesatz" in t
    # weiterhin nichts erfinden
    assert "1. Erfinde nichts." in t and "kein erfundener Fortschritt" in t


def test_live_core_names_agent_statements_as_source():
    t = core_live("Claude")
    assert "Steht etwas im Status, im Wissen oder in einer früheren [Agent]-Aussage von Claude" in t
    assert "Steht es nirgends davon: genau ein kurzer Wartesatz" in t
    assert "3. Erfinde nichts." in t and "kein erfundener Fortschritt" in t


@pytest.mark.asyncio
async def test_summary_keeps_agent_mark_for_agent_sentences():
    """Zusammenfassung: per operator.say gesprochene Sätze heißen [Claude], nicht Delta."""
    seen = {}

    async def summarize(prompt):
        seen["p"] = prompt
        return "Zusammenfassung."

    agent = RelayAgent(instructions=DEFAULT_PERSONA, speakers=None,
                       history=HistoryKeeper(1, summarize))
    agent.agent_name = "Claude"
    said = "PR 136 ist fertig, vor dem Merge MwSt in die Gratis-Zeile."
    agent.operator_said.append(said)
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="Hallo.")
    ctx.add_message(role="assistant", content=said)
    ctx.add_message(role="assistant", content="Moment, Claude schaut.")
    for k in range(4):
        ctx.add_message(role="user", content=f"Frage {k}")
        ctx.add_message(role="assistant", content=f"Antwort {k}")
    agent.history.end(list(ctx.items))
    await agent.history.task
    p = seen["p"]
    assert f"[Claude]: {said}" in p and f"Delta: {said}" not in p
    assert "Delta: Moment, Claude schaut." in p and "Nutzer: Hallo." in p
    assert "[Claude] sagte:" in p          # Prompt verlangt, die Markierung zu behalten


# ===== 2 Aktivitäts-Feed ============================================================
FAKE = ["sk_live_51Habcdefghijklmnop", "rk_live_abcdefgh12345678", "re_AbCdEf123456789",
        "whsec_abcdefghijk12345", "vhw_abcdefghijklmn12", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "token=supersecretvalue",
        "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg=="]


@pytest.mark.parametrize("secret", FAKE)
def test_worker_scrubs_fake_secrets(secret):
    out = normalize_activity({"lines": [f"17:12:03 Bash: deploy mit {secret} starten"]})
    assert secret not in out[0] and "[redacted]" in out[0]
    assert out[0].startswith("17:12:03 Bash: deploy mit ")


@pytest.mark.parametrize("ok", ["17:12:03 Bash: Tests laufen lassen", "17:12:05 Edit",
                                "17:13:00 Bash: Show working tree status",
                                "17:14:00 Agent: Review the new rest endpoint"])
def test_worker_scrub_keeps_normal_lines(ok):
    assert scrub(ok) == ok and normalize_activity({"lines": [ok]}) == [ok]


def test_activity_budget_keeps_newest_lines(monkeypatch):
    monkeypatch.delenv("VOICEHOOK_ACTIVITY_BUDGET", raising=False)
    lines = [f"17:{i:02d}:00 Bash: Schritt {i:02d} " + "wort " * 12 for i in range(15)]
    out = normalize_activity({"lines": lines})
    assert ACTIVITY_BUDGET == 800 and sum(map(len, out)) <= 800
    assert out[-1] == lines[-1].strip() and out[0] != lines[0].strip()          # älteste fallen zuerst
    assert len(normalize_activity({"lines": [f"l{i}" for i in range(40)]})) == 15
    assert normalize_activity({"lines": []}) is None and normalize_activity("x") is None
    monkeypatch.setenv("VOICEHOOK_ACTIVITY_BUDGET", "100")
    assert sum(map(len, normalize_activity({"lines": lines}))) <= 100


def test_activity_block_third_person():
    blk = activity_block(["17:12:03 Bash: Tests laufen lassen"], "Claude")
    assert blk.strip().startswith("Zuletzt hat Claude gemacht")
    assert "17:12:03 Bash: Tests laufen lassen." in blk
    assert "in der dritten Person" in blk and "ohne zu übertreiben" in blk
    assert activity_block(None, "Claude") == ""


@pytest.mark.asyncio
async def test_normal_activity_replaces_never_stacks_and_clears_on_leave():
    clock = _Clock()
    agent, h = _normal(clock)
    await h.on_agent_presence(True, "Claude")
    sizes = []
    for i in range(30):
        clock.t += 6
        await h.on_activity(_pkt({"lines": [f"17:{i:02d}:00 Bash: Schritt {i:02d}"]}))
        sizes.append(len(_last(agent)))
    t = _last(agent)
    assert len(set(sizes)) == 1                                   # Länge konstant
    assert t.count("Zuletzt hat Claude gemacht") == 1 and "Schritt 29" in t and "Schritt 28" not in t
    assert t.index("Zuletzt hat Claude gemacht") < t.index("Erinnerung: Die Regeln")  # vor dem Anker
    n = agent.update_instructions.await_count
    clock.t += 6
    await h.on_activity(_pkt({"lines": ["17:29:00 Bash: Schritt 29"]}))  # unverändert: kein Update
    assert agent.update_instructions.await_count == n
    clock.t += 6
    await h.on_activity(_pkt({"lines": []}))
    assert _last(agent) == operator_persona("Claude")             # leer: Block weg
    clock.t += 6
    await h.on_activity(_pkt({"lines": ["17:40:00 Bash: Deploy"]}))
    await h.on_agent_presence(False)
    assert _last(agent) == DEFAULT_PERSONA
    await h.on_agent_presence(True, "Claude")                     # neuer Join: kein alter Feed
    assert "Zuletzt hat" not in _last(agent)


@pytest.mark.asyncio
async def test_activity_with_board_and_budget():
    clock = _Clock()
    agent, h = _normal(clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_status(_pkt({"doing": "baut den Login"}, "operator.status"))
    lines = [f"17:{i:02d}:00 Bash: " + "ja " * 33 for i in range(15)]
    clock.t += 6
    await h.on_activity(_pkt({"lines": lines}))
    t = _last(agent)
    assert "macht gerade: baut den Login" in t and "Zuletzt hat Claude gemacht" in t
    blk = t.split("Zuletzt hat Claude gemacht", 1)[1].split(" Daraus darfst du", 1)[0]
    assert len(blk) <= ACTIVITY_BUDGET + 200


@pytest.mark.asyncio
async def test_activity_rate_limit_last_one_wins():
    clock = _Clock()
    agent, h = _normal(clock, status_interval_s=0.05)
    await h.on_agent_presence(True, "Claude")
    await h.on_activity(_pkt({"lines": ["a eins"]}))              # sofort
    assert agent.update_instructions.await_count == 2
    for w in ("zwei", "drei", "vier"):
        await h.on_activity(_pkt({"lines": [f"a {w}"]}))
    assert agent.update_instructions.await_count == 2
    clock.t += 1
    await asyncio.sleep(0.1)
    assert agent.update_instructions.await_count == 3
    t = _last(agent)
    assert "a vier." in t and "a zwei" not in t and "a drei" not in t


@pytest.mark.asyncio
async def test_activity_before_presence_counts_from_join():
    agent, h = _normal()
    await h.on_activity(_pkt({"lines": ["17:00:00 Bash: Tests laufen lassen"]}))
    await h.on_agent_presence(True, "Claude")
    assert "Zuletzt hat Claude gemacht" in _last(agent)


@pytest.mark.asyncio
async def test_live_activity_replaces_turn_in_local_context():
    agent = _live_agent()
    clock = _Clock()
    h = build_relay_handlers(MagicMock(), agent, live=True, clock=clock)
    await h.on_agent_presence(True, "Claude")
    counts = []
    for i in range(20):
        clock.t += 6
        await h.on_activity(_pkt({"lines": [f"17:{i:02d}:00 Bash: Schritt {i}"]}))
        counts.append(len(agent.chat_ctx.items))
    assert len(set(counts)) == 1
    turns = [i for i in agent.chat_ctx.items if i.id.startswith("vh-activity-")]
    assert len(turns) == 1 and turns[0].text_content.startswith("[System] Zuletzt hat Claude gemacht")
    assert "Schritt 19" in turns[0].text_content
    agent.update_instructions.assert_not_awaited()
    assert live.live_activity_user(None, "Claude").startswith("[System] Es gibt kein aktuelles")


def test_topic_routed_and_bridge_allows_it():
    assert TOPIC_ACTIVITY == "operator.activity" and TOPIC_ACTIVITY in SEND_TOPICS
    _a, h = _normal()
    assert topic_dispatch(h)[TOPIC_ACTIVITY] is h.on_activity


# ===== 3 Board-Feld faq ================================================================
def test_faq_normalized_six_pairs_200_chars():
    faq = [{"q": f"Frage {i}?", "a": f"Antwort {i}."} for i in range(9)]
    faq.insert(0, {"q": "q" * 300, "a": "a" * 300})
    b = normalize_board({"doing": "baut", "faq": faq + ["kaputt", {"q": "nur frage"}]})
    assert len(b["faq"]) == 6
    assert len(b["faq"][0]["q"]) == 200 and len(b["faq"][0]["a"]) == 200
    # Kurzformen
    assert normalize_board({"faq": ["Wann live?::Heute 18 Uhr."]})["faq"] == [
        {"q": "Wann live?", "a": "Heute 18 Uhr."}]
    assert normalize_board({"faq": [["Wann?", "Gleich."]]})["faq"] == [{"q": "Wann?", "a": "Gleich."}]
    # ohne faq: Board unverändert (Positivkontrolle gegen Altbestand)
    assert normalize_board({"doing": "a", "open": ["b"], "done": ["c"]}) == {
        "doing": "a", "open": ["b"], "done": ["c"]}


def test_faq_cut_first_under_budget(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_BOARD_BUDGET", "600")
    long = "z" * 90
    b = normalize_board({"doing": "baut", "open": [f"o{i} {long}" for i in range(2)],
                         "done": [f"d{i} {long}" for i in range(2)],
                         "faq": [{"q": f"F{i} {long}", "a": f"A{i} {long}"} for i in range(4)]})
    assert len(b["open"]) == 2 and len(b["done"]) == 2           # erst faq, dann done/open
    assert 0 < len(b["faq"]) < 4 and b["faq"][0]["q"].startswith("F0")


def test_faq_block_answer_directly_third_person():
    b = normalize_board({"doing": "baut den Login",
                         "faq": [{"q": "Wann ist das live?", "a": "Nach dem Review, heute Abend."}]})
    blk = board_block(b, "Claude")
    assert "Wahrscheinliche Fragen und Antworten von Claude" in blk
    assert "Frage: Wann ist das live? Antwort: Nach dem Review, heute Abend." in blk
    assert "antworte direkt daraus" in blk
    assert blk.rstrip().endswith("Sprich von Claude immer in der dritten Person, nie als ich.")
    only = board_block(normalize_board({"faq": [["Wer?", "Claude."]]}), "Claude")
    assert "Status von Claude" not in only and "Frage: Wer? Antwort: Claude." in only


@pytest.mark.asyncio
async def test_faq_reaches_instructions_normal_and_live():
    clock = _Clock()
    agent, h = _normal(clock)
    await h.on_agent_presence(True, "Claude")
    await h.on_status(_pkt({"doing": "baut", "faq": [["Wann live?", "Heute Abend."]]}, "operator.status"))
    assert "Frage: Wann live? Antwort: Heute Abend." in _last(agent)
    la = _live_agent()
    h2 = build_relay_handlers(MagicMock(), la, live=True, clock=_Clock())
    await h2.on_agent_presence(True, "Claude")
    await h2.on_status(_pkt({"doing": "baut", "faq": [["Wann live?", "Heute Abend."]]}, "operator.status"))
    st = [i for i in la.chat_ctx.items if i.id.startswith("vh-status-")]
    assert "Frage: Wann live? Antwort: Heute Abend." in st[0].text_content
