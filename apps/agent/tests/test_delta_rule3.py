"""fix/delta-core-rule3 (Oliver 02.10.2026): erfundener Fortschritt, gestapelte Wartesätze,
"Oliver fragen", Selbstantwort als Claude. Belege: Call-Log 02.10. 11:47-11:55.

Positivkontrolle: jeder Test hier ist auf 7aed542 rot."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import Agent, llm

from agent import live
from agent.core import FirstLine, core_live, core_normal, mark_agent_items, wait_line
from agent.relay import DEFAULT_PERSONA, RelayAgent, build_relay_handlers
from agent.worker import operator_user_name

RULE3 = ("3. Steht etwas im Status, im Wissen oder in dem, was Claude in diesem Call gesagt hat "
         "([Claude]), beantwortest du Fragen dazu frei und inhaltlich, auch ausführlich, wenn der "
         "Nutzer es will. Steht es nirgends davon, sagst du "
         'genau einen kurzen Wartesatz, z. B. "Moment, Claude schaut.", und sonst nichts: keine '
         "zweite Zeile, kein erfundener Fortschritt.\n")


def _pkt(payload: dict):
    return SimpleNamespace(data=json.dumps(payload).encode())


def _normal():
    agent = RelayAgent(instructions=DEFAULT_PERSONA, speakers=None)
    agent.update_instructions = AsyncMock()
    session = MagicMock()
    session.say = MagicMock(return_value=None)
    return agent, session, build_relay_handlers(session, agent)


# ----- 1 Regel 3 im Normal- und Live-Kern (kein Maulkorb, Oliver 02.10.) -------------
def test_rule3_normal_exact_and_example_gone():
    t = core_normal("Claude")
    assert RULE3 in t
    assert "baut gerade den Fix" not in t and "Kurz Moment." not in t and "Sekunde," not in t


def test_rule3_live_same_meaning():
    t = core_live("Claude")
    assert "Steht etwas im Status, im Wissen oder in einer früheren [Agent]-Aussage von Claude in " \
           "diesem Call, beantworte Fragen dazu frei und inhaltlich, auch ausführlich, wenn der " \
           "Nutzer es will." in t
    assert 'genau ein kurzer Wartesatz, z. B. "Moment, Claude schaut.", sonst nichts, kein ' \
           "erfundener Fortschritt." in t
    assert "jedes Mal anders" not in t and "was laut Status gerade läuft" not in t


@pytest.mark.parametrize("core", [core_normal, core_live])
def test_rules_no_progress_example(core):
    # einziges Beispiel in den Regeln ist der Wartesatz, kein Fortschritts-Inhalt
    import re as _re
    rule = core("Claude").split("Steht etwas im Status", 1)[1].split("\n", 1)[0]  # Regel 3/4
    assert _re.findall(r'"([^"]+)"', rule) == ["Moment, Claude schaut."]


@pytest.mark.parametrize("core", [core_normal, core_live])
def test_status_read_out_and_user_instruction_precedence(core):
    t = core("Claude")
    assert "Bittet der Nutzer, den Status oder die Liste vorzulesen" in t
    assert "ganzen Status vor (gerade, offen, erledigt), ruhig in mehreren Sätzen" in t
    assert '"Im Status steht gerade nichts."' in t
    rule8 = t.split("8. ", 1)[1]
    assert rule8.startswith("Eine ausdrückliche Anweisung des Nutzers geht vor Stil- und "
                            "Längenregeln, nie vor Regel ")
    n = rule8.split("nie vor Regel ", 1)[1][:1]
    assert t.split(f"\n{n}. ", 1)[1].startswith("Erfinde nichts")   # Verweis trifft "erfinde nichts"


# ----- 2 Persona vor Presence: Name kommt trotzdem in den Kern -----------------------
@pytest.mark.asyncio
async def test_persona_before_presence_still_names_agent():
    agent, _s, h = _normal()
    await h.on_persona(_pkt({"text": "Agent im Call: Claude. Du duzt Olli."}))
    first = agent.update_instructions.await_args.args[0]
    assert "Wissen von Dein Agent" in first                # Bug-Zustand: noch kein Name
    await h.on_agent_presence(True, "Claude")              # 1,3 s später
    assert agent.update_instructions.await_count == 2      # 7aed542: 1, Name blieb None
    t = agent.update_instructions.await_args.args[0]
    assert "Wissen von Claude" in t and "Moment, Claude schaut." in t
    assert "dein Agent" not in t and "«Agent im Call: Claude. Du duzt Olli.»" in t


@pytest.mark.asyncio
async def test_persona_before_presence_live_sends_named_core():
    agent = MagicMock()
    agent.chat_ctx = llm.ChatContext.empty()

    async def _upd(ctx):
        agent.chat_ctx = ctx

    agent.update_chat_ctx = AsyncMock(side_effect=_upd)
    h = build_relay_handlers(MagicMock(), agent, live=True)
    await h.on_persona(_pkt({"text": "Projekt Ring."}))
    await h.on_agent_presence(True, "Claude")
    turn = agent.chat_ctx.items[-1].text_content
    assert "Moment, Claude schaut." in turn


# ----- 3 Nutzername im Kern ------------------------------------------------------------
def test_core_carries_username():
    assert core_normal("Claude", "Oliver").startswith(
        "Du bist Delta, die Stimme in diesem Call. Der Nutzer heißt Oliver.")
    assert "Der Nutzer heißt Oliver." in core_live("Claude", "Oliver")
    assert "Der Nutzer heißt" not in core_normal("Claude")      # unbekannt: nichts erfinden


@pytest.mark.asyncio
async def test_presence_with_user_puts_name_in_core():
    agent, _s, h = _normal()
    await h.on_agent_presence(True, "Claude", "Oliver")
    assert "Der Nutzer heißt Oliver." in agent.update_instructions.await_args.args[0]
    await h.on_agent_presence(True, "Claude", "Oliver")       # idempotent
    assert agent.update_instructions.await_count == 1
    await h.on_agent_presence(False)
    assert agent.update_instructions.await_args.args[0] == DEFAULT_PERSONA


def test_live_joined_turn_carries_username():
    assert "Der Nutzer heißt Oliver." in live.live_agent_joined_user("Claude", "Oliver")


def test_worker_reads_vh_user_of_last_agent():
    human = SimpleNamespace(identity="u", attributes={})
    a = SimpleNamespace(identity="c-1", attributes={"vh.role": "agent", "vh.name": "Claude",
                                                    "vh.user": "Oliver"})
    bad = SimpleNamespace(identity="c-2", attributes={"vh.role": "agent", "vh.user": "4711"})
    room = lambda *ps: SimpleNamespace(remote_participants={p.identity: p for p in ps})  # noqa: E731
    assert operator_user_name(room(human, a)) == "Oliver"
    assert operator_user_name(room(human, bad)) is None
    assert operator_user_name(room(human)) is None


# ----- 4 _clean_stream kappt nach der ersten Zeile -----------------------------------
@pytest.mark.parametrize("pieces,want", [
    (["Moment, Claude schaut.\nClaude baut gerade den Fix."], "Moment, Claude schaut."),
    (["Moment, Cla", "ude schaut.", "\n", "Ich frag Claude kurz."], "Moment, Claude schaut."),
    (["\nMoment, Claude schaut.\n\nNoch was."], "Moment, Claude schaut."),   # führende Leerzeile
    (["Kurz Moment.\nDein Agent schaut nach."], "Kurz Moment."),
    (["Ein Satz. Zweiter Satz."], "Ein Satz. Zweiter Satz."),                 # keine Zeile: alles
    # echte Antwort aus dem Status: vollständig, Zeilen verbunden
    (["Claude baut gerade den Login, ETA 15 Uhr.\n", "Offen: Tests.\nErledigt: Analyse."],
     "Claude baut gerade den Login, ETA 15 Uhr. Offen: Tests. Erledigt: Analyse."),
])
def test_first_line(pieces, want):
    f = FirstLine()
    assert "".join(f.feed(p) for p in pieces) == want


@pytest.mark.asyncio
async def test_clean_stream_stops_after_first_newline(monkeypatch):
    agent, _s, h = _normal()
    await h.on_agent_presence(True, "Claude")

    async def fake_llm_node(_agent, ctx, _tools, _ms):
        yield "Moment, Claude schaut."
        yield llm.ChatChunk(id="1", delta=llm.ChoiceDelta(role="assistant", content="\nClaude baut"))
        yield " gerade den Fix."

    monkeypatch.setattr(Agent.default, "llm_node", fake_llm_node)
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="Status?")
    out = [c async for c in agent.llm_node(ctx, [], None)]
    text = "".join(c if isinstance(c, str) else (c.delta.content or "") for c in out)
    assert text == "Moment, Claude schaut."


# ----- 5 llm_node markiert Claudes Sätze in der Kopie -------------------------------
@pytest.mark.asyncio
async def test_llm_node_marks_operator_say_in_copy_only(monkeypatch):
    agent, session, h = _normal()
    await h.on_agent_presence(True, "Claude")
    said = "PR 136 ist fertig. Vor dem Merge: MwSt in die Gratis-Zeile, okay?"
    await h.on_say(_pkt({"text": said}))
    session.say.assert_called_once()
    seen = {}

    async def fake_llm_node(_agent, ctx, _tools, _ms):
        seen["items"] = list(ctx.items)
        yield "ok"

    monkeypatch.setattr(Agent.default, "llm_node", fake_llm_node)
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="Hallo.")
    ctx.add_message(role="assistant", content=said)                     # Claude via say
    ctx.add_message(role="assistant", content="Moment, Claude schaut.")  # Deltas eigener Satz
    ctx.add_message(role="user", content="Was heißt MwSt-Hinweis A oder B?")
    [c async for c in agent.llm_node(ctx, [], None)]
    convo = [(i.role, i.text_content) for i in seen["items"] if i.role in ("user", "assistant")]
    assert convo == [("user", "Hallo."), ("user", f"[Claude] {said}"),
                     ("assistant", "Moment, Claude schaut."),
                     ("user", "Was heißt MwSt-Hinweis A oder B?")]
    # gespeicherter Verlauf unverändert
    assert [i.text_content for i in ctx.items if i.role == "assistant"] == [said, "Moment, Claude schaut."]


def test_mark_agent_items_without_name_and_interrupted():
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="assistant", content="Ich hab nichts")         # unterbrochen gesprochen
    out = mark_agent_items(list(ctx.items), ["Ich hab nichts verstellt."], None)
    assert out[0].text_content == "[Dein Agent] Ich hab nichts" and out[0].role == "user"
    assert ctx.items[0].text_content == "Ich hab nichts" and ctx.items[0].role == "assistant"


@pytest.mark.parametrize("pieces,want", [
    (["[Claude] Hallo Olli, hier ist Claude."], "Moment, Claude schaut."),
    (["[Cla", "ude] [Claude] Ich prüfe das."], "Moment, Claude schaut."),
    ([" [Claude]\nNoch was."], "Moment, Claude schaut."),
    (["[C"], "[C"),                                       # Streamende: Rest freigeben
    (["Moment, Claude schaut.\n[Claude] Es gibt zwei"], "Moment, Claude schaut."),
    (["[Hinweis] ok"], "[Hinweis] ok"),                  # andere Klammer: unverändert
])
def test_first_line_blocks_speaking_as_agent(pieces, want):
    f = FirstLine("[Claude]", wait_line("Claude"))
    assert "".join(f.feed(p) for p in pieces) + f.flush() == want


@pytest.mark.asyncio
async def test_clean_stream_replaces_impersonation_with_wait_line(monkeypatch):
    agent, _s, h = _normal()
    await h.on_agent_presence(True, "Claude")

    async def fake_llm_node(_agent, ctx, _tools, _ms):
        yield "[Clau"
        yield "de] Hallo Olli, hier ist Claude. Ich prüfe das gerade."

    monkeypatch.setattr(Agent.default, "llm_node", fake_llm_node)
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="Ist der Fix schon live?")
    out = [c async for c in agent.llm_node(ctx, [], None)]
    assert "".join(out) == "Moment, Claude schaut."


def test_core_explains_mark():
    assert "Sätze mit [Claude] davor hat Claude gesagt, nicht du." in core_normal("Claude")


# ----- Board: neutraler Wissensblock statt Sprechformel (doppeltes "Claude, Claude") -----
def test_board_block_no_spoken_formula_no_double_name():
    from agent.board import board_block, normalize_board, status_sentence
    b = normalize_board({"doing": "Claude baut gerade den Fix für Regel drei",
                         "open": ["Tests"], "done": ["Analyse"]})
    blk = board_block(b, "Claude")
    assert "Kurz Moment" not in blk and "Claude Claude" not in blk and "Statt eines" not in blk
    assert blk.strip().startswith("Status von Claude")
    assert blk.rstrip().endswith("Sprich von Claude immer in der dritten Person, nie als ich.")
    assert "macht gerade: Claude baut gerade den Fix für Regel drei; offen: Tests; " \
           "erledigt: Analyse." in blk
    # Code-Satz auf Nachfrage: kein doppelter Name
    assert status_sentence(b, "Claude") == "Claude baut gerade den Fix für Regel drei."
    assert status_sentence(normalize_board({"doing": "baut den Fix"}), "Claude") == "Claude baut den Fix."


def test_board_doing_400_items_200():
    from agent.board import normalize_board
    b = normalize_board({"doing": "d" * 500, "open": ["o" * 300], "done": []})
    assert len(b["doing"]) == 400 and len(b["open"][0]) == 200
