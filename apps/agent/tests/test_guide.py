"""Delta ab Werk als voicehook-Experte (Oliver 01.10.2026)."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import live
from agent.core import core_normal
from agent.guide import SKILL_URL, VOICEHOOK_GUIDE
from agent.relay import (
    DEFAULT_PERSONA,
    OPERATOR_PERSONA,
    TOPIC_PERSONA,
    RelayAgent,
    build_relay_handlers,
)

PROMPTS = {"normal": DEFAULT_PERSONA, "live": live.LIVE_BASE_INSTRUCTIONS}

CORE_FACTS = (
    "voicehook.ai",
    "Delta",
    # Oliver 01.10.: Produkt
    "jederzeit mit seinen eigenen KI-Agents",
    "telefonieren",
    "auf einer eigenen Maschine läuft und Software installieren kann",
    "Claude Code", "Hermes", "Codex",
    "Einladungslink",
    # Onboarding-Dialog: ein klarer nächster Schritt (Einladen)
    "genau einem nächsten Schritt",
    "Lad jetzt mal deinen Agent ein. Hast du einen Hermes oder irgendwo Claude Code laufen?",
    "nur einen Agent mit einer Umgebung, in der er Software installieren kann",
    "Agent einladen",            # Knopf-Beschriftung wie in web/voice.html
    "kopiert den Einladungstext",
    "folgt dem Einladungslink und nutzt den Skill",
    "voicehook.ai/agent/SKILL.md",
    # Modi (gegengeprüft 02.10.: kein Tonfall, kein Stimmenfilter, kein "wörtlich")
    "Live: natürlicher, schneller und wechselt automatisch die Sprache",
    "Normal: günstiger für lange Sessions",
    # Musik + Designs (web/voice.html: mode.music, dp.btn, style.*)
    "Knopf Musik", "Visualizer", "Knopf Design", "Wasser", "Tiger", "Hell oder",
    # Gratis, Login, Datenschutz, Grenzen
    "Gratis-Verbrauch ohne Anmeldung", "Google, GitHub oder per E-Mail-Link",
    "Deepgram und Google", "nicht dauerhaft", "keinen Zugriff auf Rechner",
    # Kosten (Oliver 01.10.)
    "tokenbasiert nach echtem Verbrauch",
    "Prepaid, kein Abo",
    "so viel man möchte",
    "sehr günstig", "fairer Dienst",
    "ein, zwei Sätze",
    "wofür dein Gegenüber voicehook einsetzen will",
)


@pytest.mark.parametrize("mode", PROMPTS)
def test_base_prompt_contains_core_facts(mode):
    t = PROMPTS[mode]
    for fact in CORE_FACTS:
        assert fact in t, (mode, fact)


def test_live_and_normal_carry_identical_guide():
    # eine Quelle, beide Modi: gleicher Text, je genau einmal
    for t in PROMPTS.values():
        assert t.count(VOICEHOOK_GUIDE) == 1


def test_skill_url_matches_spoken_form():
    assert SKILL_URL == "https://voicehook.ai/agent/SKILL.md"
    assert SKILL_URL.removeprefix("https://") in VOICEHOOK_GUIDE


PRICE = re.compile(
    r"\d+([.,]\d+)?\s*(€|eur\b|euro|cent|ct\b|\$|usd|dollar)"
    r"|cent\s+(pro|je|die)\s+minute|ab\s+ein\s+paar\s+cent|pro\s+minute\s+\d",
    re.IGNORECASE,
)


@pytest.mark.parametrize("mode", PROMPTS)
def test_no_concrete_prices(mode):
    t = PROMPTS[mode]
    # Positivkontrolle: das Muster findet echte und vage Preisangaben (Review high)
    for bad in ("8 Cent pro Minute", "5 Euro", "ab ein paar Cent pro Minute", "0,08 €"):
        assert PRICE.search(bad), bad
    assert not PRICE.search(t), PRICE.search(t)
    assert "€" not in t and "$" not in t
    assert "Nenn nie konkrete Preise" in t


@pytest.mark.parametrize("mode", PROMPTS)
def test_no_invented_claims(mode):
    t = PROMPTS[mode]
    # "gratis" ist seit dem täglichen Gratis-Verbrauch (freetier.py) eine echte Angabe
    for bad in ("kostenlos", "unbegrenzt", "Abo-Modell", "Flatrate", "garantiert",
                "Minuten gratis", "Free-Tier", "Video", "Bildschirm teilen",
                "vollem Zugriff", "Cursor"):
        assert bad.lower() not in t.lower(), (mode, bad)
    assert "Erfinde nichts." in t                      # Kern-Regel 1 (core.py)
    assert "gibt es für dich nicht" in t               # Guide: nichts außerhalb des Wissens


@pytest.mark.parametrize("mode", PROMPTS)
def test_brevity_rule_never_cuts_operator_statements(mode):
    # Review medium: Kürze/Nachfrage nur für eigene Antworten, Operator-Aussagen vollständig
    t = PROMPTS[mode]
    assert ("Die Kürze- und Nachfrage-Regel gilt nur für deine eigenen Antworten, nie für "
            "Aussagen des Agenten; die sprichst du vollständig und ohne Nachsatz.") in t


@pytest.mark.parametrize("mode", PROMPTS)
def test_operator_rules_kept_and_scoped(mode):
    t = PROMPTS[mode]
    assert "Moment, dein Agent schaut." in t
    assert "beantwortest du nie selbst" in t
    # ohne Agent: voicehook-Fragen aus dem Wissen, fremde Fähigkeiten nicht behaupten
    assert "Solange kein Agent im Raum ist" in t
    assert "Fragen zu voicehook selbst aus diesem Wissen" in t
    assert "die nicht voicehook selbst betreffen, beantwortest du auch dann nicht" in t
    # Delta-Kern: keine Persona ersetzt mehr irgendetwas (Oliver 02.10.)
    assert "ersetzt sie diese Werksrolle vollständig" not in t


def test_no_dashes_in_guide():
    assert "–" not in VOICEHOOK_GUIDE and "—" not in VOICEHOOK_GUIDE


def test_guide_drops_disproved_mode_claims():
    """Recherche 02.10.: Tonfall (gemini-3.8-live: affective dialogue entfernt),
    Stimmenfilter und "wörtlich" nicht mehr versprechen; keine internen Töpfe."""
    for bad in ("Tonfall", "fremde Stimmen", "kommen wörtlich", "Stille nichts",
                "Topf", "Deckel", "Marketing", "Eis,", "unter Style", "oben rechts macht"):
        assert bad not in VOICEHOOK_GUIDE, bad


@pytest.mark.asyncio
async def test_normal_persona_push_replaces_guide_but_keeps_core():
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    agent.update_instructions = AsyncMock()
    h = build_relay_handlers(MagicMock(), agent)

    class _Pkt:
        topic = TOPIC_PERSONA
        data = json.dumps({"text": "Du bist Coach"}).encode()

    await h.on_persona(_Pkt())
    t = agent.update_instructions.await_args.args[0]
    assert t.startswith(core_normal(None)) and "«Du bist Coach»" in t
    assert VOICEHOOK_GUIDE not in t


def test_live_persona_push_replaces_factory_role():
    u = live.LIVE_PERSONA_USER.format(text="Du bist Coach")
    assert u.startswith("[System]") and "statt deiner Werksrolle" in u
    assert "«Du bist Coach»" in u and "keine Regeln" in u


# ----- Werksrolle aus bei Agent-Join, an bei -Leave (Oliver 01.10., Review medium) ----
def _normal_handlers():
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    agent.update_instructions = AsyncMock()
    return agent, build_relay_handlers(MagicMock(), agent)


def _live_handlers():
    agent = MagicMock()
    turns: list[str] = []

    class _Ctx:
        def copy(self):
            return self

        def add_message(self, role, content):
            assert role == "user"
            turns.append(content)

    agent.chat_ctx = _Ctx()
    agent.update_chat_ctx = AsyncMock()
    agent.update_instructions = AsyncMock()
    return agent, turns, build_relay_handlers(MagicMock(), agent, live=True)


def _persona_pkt(text):
    class _Pkt:
        topic = TOPIC_PERSONA
        data = json.dumps({"text": text}).encode()
    return _Pkt()


def test_neutral_prompts_carry_no_guide():
    for t in (OPERATOR_PERSONA, live.LIVE_CORE_INSTRUCTIONS):
        assert VOICEHOOK_GUIDE not in t and "Werksrolle" not in t and "Verkäufer" not in t
        assert "Moment, dein Agent schaut." in t
    # Positivkontrolle: die Werks-Prompts tragen den Guide
    assert VOICEHOOK_GUIDE in DEFAULT_PERSONA and VOICEHOOK_GUIDE in live.LIVE_BASE_INSTRUCTIONS
    assert live.LIVE_BASE_INSTRUCTIONS.startswith(live.LIVE_CORE_INSTRUCTIONS)


@pytest.mark.asyncio
async def test_normal_agent_join_and_leave_switch_role():
    agent, h = _normal_handlers()
    await h.on_agent_presence(False)                 # niemand da: nichts umschalten
    agent.update_instructions.assert_not_awaited()
    await h.on_agent_presence(True)
    agent.update_instructions.assert_awaited_once_with(OPERATOR_PERSONA)
    await h.on_agent_presence(True)                  # zweiter Agent / Attribut-Event: idempotent
    assert agent.update_instructions.await_count == 1
    await h.on_agent_presence(False)
    assert agent.update_instructions.await_args_list[-1].args == (DEFAULT_PERSONA,)


@pytest.mark.asyncio
async def test_live_agent_join_and_leave_switch_role_via_user_turn():
    agent, turns, h = _live_handlers()
    await h.on_agent_presence(True)
    await h.on_agent_presence(False)
    agent.update_instructions.assert_not_awaited()   # nie model-Turn (realtime_api.py Z. 646-675)
    assert turns == [live.LIVE_AGENT_JOINED_USER, live.LIVE_AGENT_LEFT_USER]
    assert turns[0].startswith("[System]") and live.LIVE_CORE_INSTRUCTIONS in turns[0]
    assert VOICEHOOK_GUIDE not in turns[0] and "gilt ab sofort nicht mehr" in turns[0]
    assert turns[1].startswith("[System]") and turns[1].endswith(VOICEHOOK_GUIDE)


@pytest.mark.asyncio
async def test_persona_push_wins_over_join_switch_and_leave_restores_guide():
    agent, h = _normal_handlers()
    await h.on_persona(_persona_pkt("Du bist Coach"))
    await h.on_agent_presence(True)                  # Persona bleibt, Kern neu gesetzt
    assert agent.update_instructions.await_count == 2
    await h.on_persona(_persona_pkt("Du bist Tutor"))
    await h.on_agent_presence(False)                 # Agent weg: Werksrolle wieder an
    got = [c.args[0] for c in agent.update_instructions.await_args_list]
    assert "«Du bist Coach»" in got[0] and "«Du bist Coach»" in got[1]
    assert "«Du bist Tutor»" in got[2] and got[3] == DEFAULT_PERSONA
    assert all(g.startswith(core_normal(None)) for g in got)


@pytest.mark.asyncio
async def test_persona_after_join_replaces_neutral_role():
    _agent, turns, h = _live_handlers()
    await h.on_agent_presence(True)
    await h.on_persona(_persona_pkt("Du bist Coach"))
    assert turns[-1] == live.LIVE_PERSONA_USER.format(text="Du bist Coach")


def test_operator_detection_uses_vh_role_attribute():
    from types import SimpleNamespace

    from agent.worker import is_operator_agent, operator_agent_present

    op = SimpleNamespace(identity="claude-x", attributes={"vh.role": "agent", "vh.name": "Claude"})
    human = SimpleNamespace(identity="user-1", attributes={})
    bare = SimpleNamespace(identity="hermes-1")
    assert is_operator_agent(op) and not is_operator_agent(human) and not is_operator_agent(bare)
    assert operator_agent_present(SimpleNamespace(remote_participants={"a": human, "b": op}))
    assert not operator_agent_present(SimpleNamespace(remote_participants={"a": human}))


def _run_worker(monkeypatch, *, live_mode, initial):
    """Treibt worker.entrypoint mit Fake-Raum; liefert (room, session, agent_ref)."""
    import asyncio
    from types import SimpleNamespace

    import agent.worker as w
    from agent import freetier

    class _Emitter:
        def __init__(self):
            self.handlers = {}

        def on(self, event, fn=None):
            if fn is None:
                return lambda f: self.on(event, f)
            self.handlers.setdefault(event, []).append(fn)
            return fn

        def emit(self, event, *args):
            for fn in self.handlers.get(event, []):
                fn(*args)

    if live_mode:
        monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
        freetier.register_room("r1", "live", [], exempt=True)
    else:
        monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
        monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
        freetier.register_room("r1", "normal", [], exempt=True)  # fail-closed seit PR #93
    session = _Emitter()
    session.start = AsyncMock()
    session.aclose = AsyncMock()
    monkeypatch.setattr(w, "build_session", lambda: session)
    calls: list[tuple[str, str]] = []

    async def _upd_instr(self, text):
        calls.append(("instructions", text))

    async def _upd_ctx(self, ctx):
        calls.append(("chat_ctx", ctx.items[-1].text_content))

    monkeypatch.setattr(w.RelayAgent, "update_instructions", _upd_instr)
    monkeypatch.setattr(w.RelayAgent, "update_chat_ctx", _upd_ctx)
    room = _Emitter()
    room.name = "r1"
    room.remote_participants = dict(initial)
    room.local_participant = SimpleNamespace(identity="voice-ai", publish_data=AsyncMock())
    ctx = SimpleNamespace(connect=AsyncMock(), room=room, job=SimpleNamespace(id="j1"),
                          shutdown=MagicMock(), delete_room=AsyncMock())
    op = SimpleNamespace(identity="claude-x", attributes={"vh.role": "agent"},
                         kind=None)

    async def _go():
        await w.entrypoint(ctx)
        await asyncio.sleep(0)
        if not initial:
            room.remote_participants["op"] = op
            room.emit("participant_connected", op)
            for _ in range(3):
                await asyncio.sleep(0)
        room.remote_participants.pop("op", None)
        room.emit("participant_disconnected", op)
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(_go())
    return calls, op


@pytest.mark.parametrize("live_mode", [False, True])
def test_worker_switches_role_on_agent_join_and_leave(monkeypatch, live_mode):
    calls, _op = _run_worker(monkeypatch, live_mode=live_mode, initial={})
    if live_mode:
        assert calls == [("chat_ctx", live.LIVE_AGENT_JOINED_USER),
                         ("chat_ctx", live.LIVE_AGENT_LEFT_USER)]
    else:
        assert calls == [("instructions", OPERATOR_PERSONA), ("instructions", DEFAULT_PERSONA)]


def test_worker_switches_role_when_agent_was_there_first(monkeypatch):
    from types import SimpleNamespace

    op = SimpleNamespace(identity="claude-x", attributes={"vh.role": "agent"}, kind=None)
    calls, _ = _run_worker(monkeypatch, live_mode=False, initial={"op": op})
    assert calls == [("instructions", OPERATOR_PERSONA), ("instructions", DEFAULT_PERSONA)]


def test_worker_ignores_humans_for_role_switch(monkeypatch):
    # Positivkontrolle zur Join-Erkennung: ein Mensch schaltet nichts um
    from types import SimpleNamespace

    human = SimpleNamespace(identity="user-1", attributes={}, kind=None)
    calls, _ = _run_worker(monkeypatch, live_mode=False, initial={"h": human})
    assert calls == []
