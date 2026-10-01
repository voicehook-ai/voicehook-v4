"""Delta ab Werk als voicehook-Experte (Oliver 01.10.2026)."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import live
from agent.guide import SKILL_URL, VOICEHOOK_GUIDE
from agent.relay import DEFAULT_PERSONA, TOPIC_PERSONA, RelayAgent, build_relay_handlers

PROMPTS = {"normal": DEFAULT_PERSONA, "live": live.LIVE_BASE_INSTRUCTIONS}

CORE_FACTS = (
    "voicehook.ai",
    "Delta",
    "eigenen KI-Agent",
    "Claude Code", "Codex", "Hermes",
    "Einladungslink",
    "statt zu tippen",
    "vollem Zugriff auf seine eigenen Tools",
    "Agent einladen",            # Knopf-Beschriftung wie in web/voice.html
    "Einladungstext",
    "voicehook.ai/agent/SKILL.md",
    "Live:", "natürlichere Stimme", "schneller", "Tonfall",
    "Normal:", "günstiger", "fremde Stimmen und Nebengeräusche", "wörtlich",
    "Stille kostet nichts an Spracherkennung",
    "ab ein paar Cent pro Minute, abhängig vom Modus",
    "1 bis 3 Sätze",
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


@pytest.mark.parametrize("mode", PROMPTS)
def test_no_concrete_prices(mode):
    t = PROMPTS[mode]
    # Positivkontrolle: das Muster findet einen echten Preis
    price = re.compile(r"\d+([.,]\d+)?\s*(€|eur\b|euro|cent|ct\b|\$|usd|dollar)", re.IGNORECASE)
    assert price.search("8 Cent pro Minute") and price.search("5 Euro")
    assert not price.search(t), price.search(t)
    assert "€" not in t and "$" not in t
    assert "Nenn nie konkrete Preise" in t


@pytest.mark.parametrize("mode", PROMPTS)
def test_no_invented_claims(mode):
    t = PROMPTS[mode]
    for bad in ("kostenlos", "gratis", "unbegrenzt", "Abo-Modell", "Flatrate", "garantiert",
                "Minuten gratis", "Free-Tier", "Video", "Bildschirm teilen"):
        assert bad.lower() not in t.lower(), (mode, bad)
    assert "Erfinde nichts dazu" in t


@pytest.mark.parametrize("mode", PROMPTS)
def test_operator_rules_kept_and_scoped(mode):
    t = PROMPTS[mode]
    assert "Moment, ich schau nach." in t
    assert "Ist ein Operator im Raum, gilt: Fragen nach" in t
    # ohne Operator: voicehook-Fragen aus dem Wissen, fremde Fähigkeiten nicht behaupten
    assert "Solange kein Operator im Raum ist" in t
    assert "Fragen zu voicehook selbst aus diesem Wissen" in t
    assert "die nicht voicehook selbst betreffen, beantwortest du auch dann nicht" in t
    assert "ersetzt sie diese Werksrolle vollständig" in t


def test_no_dashes_in_guide():
    assert "–" not in VOICEHOOK_GUIDE and "—" not in VOICEHOOK_GUIDE


@pytest.mark.asyncio
async def test_normal_persona_push_replaces_guide():
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    agent.update_instructions = AsyncMock()
    h = build_relay_handlers(MagicMock(), agent)

    class _Pkt:
        topic = TOPIC_PERSONA
        data = json.dumps({"text": "Du bist Coach"}).encode()

    await h.on_persona(_Pkt())
    agent.update_instructions.assert_awaited_once_with("Du bist Coach")


def test_live_persona_push_replaces_factory_role():
    u = live.LIVE_PERSONA_USER.format(text="Du bist Coach")
    assert u.startswith("[Operator]") and "statt deiner Werksrolle" in u
    assert "zusätzlich" not in u
