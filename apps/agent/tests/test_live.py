"""Gemini-Live-Testmodus: Modell-Fabrik, Kostenrechnung, Relay im Live-Modus."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import live
from agent.relay import TOPIC_SAY, RelayAgent, build_relay_handlers


def _metrics(audio_in=0, text_in=0, cached=0, audio_out=0, text_out=0):
    return SimpleNamespace(
        input_tokens=audio_in + text_in + cached,
        output_tokens=audio_out + text_out,
        input_token_details=SimpleNamespace(audio_tokens=audio_in, text_tokens=text_in, cached_tokens=cached),
        output_token_details=SimpleNamespace(audio_tokens=audio_out, text_tokens=text_out),
    )


def test_cost_uses_published_gemini_38_live_prices():
    # 1M je Kategorie = Listenpreis (ai.google.dev/gemini-api/docs/pricing, 30.09.2026)
    assert live.live_cost_usd(_metrics(audio_in=1_000_000)) == pytest.approx(3.00)
    assert live.live_cost_usd(_metrics(text_in=1_000_000)) == pytest.approx(0.75)
    assert live.live_cost_usd(_metrics(audio_out=1_000_000)) == pytest.approx(12.00)
    assert live.live_cost_usd(_metrics(text_out=1_000_000)) == pytest.approx(4.50)


def test_cost_counts_cached_tokens_as_audio_input_conservatively():
    assert live.live_cost_usd(_metrics(cached=1_000_000)) == pytest.approx(3.00)


def test_cost_tolerates_missing_details():
    m = SimpleNamespace(input_tokens=1_000_000, output_tokens=0,
                        input_token_details=None, output_token_details=None)
    assert live.live_cost_usd(m) == pytest.approx(3.00)   # unbekannt -> teuerster Input


_COMPRESS_ENVS = ("VOICEHOOK_LIVE_COMPRESS_TRIGGER", "VOICEHOOK_LIVE_COMPRESS_TARGET",
                  "VOICEHOOK_LIVE_TRIGGER_TOKENS", "VOICEHOOK_LIVE_TARGET_TOKENS")


def _cwc(monkeypatch, **env):
    captured = {}
    monkeypatch.setattr(live, "_realtime_model_cls", lambda: (lambda **kw: captured.update(kw) or "M"))
    for k in _COMPRESS_ENVS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    live.build_live_llm()
    c = captured["context_window_compression"]
    return c.trigger_tokens, c.sliding_window.target_tokens


def test_l4_compress_env_overrides(monkeypatch):
    assert _cwc(monkeypatch) == (15000, 7000)
    assert _cwc(monkeypatch, VOICEHOOK_LIVE_COMPRESS_TRIGGER="20000",
                VOICEHOOK_LIVE_COMPRESS_TARGET="9000") == (20000, 9000)
    # alte Namen gelten weiter, neue gewinnen
    assert _cwc(monkeypatch, VOICEHOOK_LIVE_TRIGGER_TOKENS="18000") == (18000, 7000)
    assert _cwc(monkeypatch, VOICEHOOK_LIVE_TRIGGER_TOKENS="18000",
                VOICEHOOK_LIVE_COMPRESS_TRIGGER="16000") == (16000, 7000)


@pytest.mark.parametrize("env", [
    {"VOICEHOOK_LIVE_COMPRESS_TRIGGER": "abc"},                         # kaputt
    {"VOICEHOOK_LIVE_COMPRESS_TARGET": "-5"},                           # <= 0
    {"VOICEHOOK_LIVE_COMPRESS_TARGET": "0"},
    {"VOICEHOOK_LIVE_COMPRESS_TRIGGER": "6000"},                        # target >= trigger
    {"VOICEHOOK_LIVE_COMPRESS_TRIGGER": "8000", "VOICEHOOK_LIVE_COMPRESS_TARGET": "8000"},
])
def test_l4_compress_bad_env_falls_back(monkeypatch, env):
    trigger, target = _cwc(monkeypatch, **env)
    assert (trigger, target) == (15000, 7000) and target < trigger


def test_build_live_llm_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(live, "_realtime_model_cls", lambda: (lambda **kw: captured.update(kw) or "MODEL"))
    for k in ("VOICEHOOK_LIVE_MODEL", "VOICEHOOK_LIVE_VOICE", *_COMPRESS_ENVS):
        monkeypatch.delenv(k, raising=False)
    assert live.build_live_llm() == "MODEL"
    assert captured["model"] == "gemini-3.8-live"
    assert "language" not in captured              # native audio ignoriert language_code
    cwc = captured["context_window_compression"]
    assert cwc.trigger_tokens == 15000 and cwc.sliding_window.target_tokens == 7000  # L4
    assert captured["session_resumption"] is not None
    assert not captured["session_resumption"].transparent   # Developer API lehnt transparent ab


def test_build_live_llm_env_overrides(monkeypatch):
    captured = {}
    monkeypatch.setattr(live, "_realtime_model_cls", lambda: (lambda **kw: captured.update(kw) or "M"))
    monkeypatch.setenv("VOICEHOOK_LIVE_MODEL", "gemini-3.8-live-extended-thinking")
    monkeypatch.setenv("VOICEHOOK_LIVE_TRIGGER_TOKENS", "20000")
    monkeypatch.setenv("VOICEHOOK_LIVE_TARGET_TOKENS", "8000")
    live.build_live_llm()
    assert captured["model"] == "gemini-3.8-live-extended-thinking"
    assert captured["context_window_compression"].trigger_tokens == 20000


# ── Relay im Live-Modus: operator.say wird Anweisung, nicht wörtliches TTS ──

@dataclass
class _Pkt:
    topic: str
    data: bytes


@pytest.mark.asyncio
async def test_live_say_becomes_generate_reply_instruction():
    session = MagicMock()
    session.generate_reply = MagicMock(return_value=None)
    h = build_relay_handlers(session, RelayAgent(instructions="x"), live=True)
    await h.on_say(_Pkt(TOPIC_SAY, json.dumps({"text": "Termin ist Dienstag"}).encode()))
    session.say.assert_not_called()
    kwargs = session.generate_reply.call_args.kwargs
    # als User-Turn, NICHT instructions= (das würde ein role="model"-Turn)
    assert "instructions" not in kwargs
    assert kwargs["user_input"].startswith("[Agent]") and "Termin ist Dienstag" in kwargs["user_input"]
    assert kwargs["allow_interruptions"] is True


@pytest.mark.asyncio
async def test_live_say_does_not_publish_operator_text_as_agent_transcript():
    session, room = MagicMock(), MagicMock()
    session.generate_reply = MagicMock(return_value=None)
    h = build_relay_handlers(session, RelayAgent(instructions="x"), room=room, live=True)
    await h.on_say(_Pkt(TOPIC_SAY, json.dumps({"text": "hallo"}).encode()))
    # im Live-Modus formuliert Gemini selbst; das echte Gesprochene kommt über
    # conversation_item_added, nicht der Operator-Text
    room.local_participant.publish_data.assert_not_called()


def _m(kind, **kw):
    return type(kind, (), kw)()


def test_metric_cost_pipeline_components():
    assert live.metric_cost_usd(_m("STTMetrics", audio_duration=60.0)) == pytest.approx(0.0077)
    assert live.metric_cost_usd(_m("TTSMetrics", characters_count=1_000_000)) == pytest.approx(30.0)
    assert live.metric_cost_usd(_m("LLMMetrics", prompt_tokens=1_000_000, completion_tokens=0)) == pytest.approx(0.30)
    assert live.metric_cost_usd(_m("LLMMetrics", prompt_tokens=0, completion_tokens=1_000_000)) == pytest.approx(2.50)


def test_metric_cost_unknown_type_is_zero():
    assert live.metric_cost_usd(_m("VADMetrics")) == 0.0


@pytest.mark.asyncio
async def test_live_persona_is_user_turn_not_update_instructions():
    from unittest.mock import AsyncMock
    agent = RelayAgent(instructions="basis")
    agent.update_instructions = AsyncMock()
    agent.update_chat_ctx = AsyncMock()
    h = build_relay_handlers(MagicMock(), agent, live=True)
    await h.on_persona(_Pkt("operator.persona", json.dumps({"text": "Du bist Coach"}).encode()))
    agent.update_instructions.assert_not_called()
    ctx = agent.update_chat_ctx.call_args.args[0]
    last = ctx.items[-1]
    assert last.role == "user" and "[System]" in last.text_content and "Du bist Coach" in last.text_content


def test_live_base_instructions_pin_voice_and_language():
    t = live.LIVE_BASE_INSTRUCTIONS
    assert "Deutsch" in t and "dieselbe ruhige, warme Stimme" in t
    assert "Nachrichten mit [Agent]" in t and "Nachrichten mit [System]" in t


# ── Kosten mit offengelegter Basis: Menge aus Metrik x geprüfter Preis ──

def test_prices_match_official_pages_as_of_date():
    # Positivkontrolle gegen die am 30.09.2026 geprüften Preisseiten
    assert live.PRICES_AS_OF == "2026-09-30"
    assert pytest.approx(0.0077) == live.PRICE_STT_PER_S * 60      # Deepgram nova-3 regulär
    assert pytest.approx(0.00003) == live.PRICE_TTS_PER_CHAR       # Chirp 3: HD
    assert pytest.approx(0.30) == live.PRICE_LLM_IN * 1e6          # gemini-2.5-flash Text in
    assert pytest.approx(2.50) == live.PRICE_LLM_OUT * 1e6
    assert (live.PRICE_AUDIO_IN, live.PRICE_TEXT_IN) == (3.00, 0.75)
    assert (live.PRICE_AUDIO_OUT, live.PRICE_TEXT_OUT) == (12.00, 4.50)


def _rt(audio_in=0, text_in=0, audio_out=0, text_out=0):
    return _m("RealtimeModelMetrics", **vars(_metrics(audio_in=audio_in, text_in=text_in,
                                                      audio_out=audio_out, text_out=text_out)))


def test_meter_pipeline_sums_and_basis():
    meter = live.CostMeter("pipeline")
    meter.add(_m("STTMetrics", audio_duration=30.0))
    meter.add(_m("STTMetrics", audio_duration=30.0))
    meter.add(_m("TTSMetrics", characters_count=1000))
    meter.add(_m("LLMMetrics", prompt_tokens=2000, completion_tokens=100))
    meter.add(_m("VADMetrics"))   # unbekannt: kein Betrag, keine Basis
    expected = 60 * 0.0077 / 60 + 1000 * 30 / 1e6 + (2000 * 0.30 + 100 * 2.50) / 1e6
    p = meter.payload()
    assert p["usd"] == pytest.approx(round(expected, 5))
    assert p["mode"] == "pipeline" and p["prices_as_of"] == "2026-09-30"
    assert p["basis"] == {"stt_audio_s": 60.0, "tts_chars": 1000, "llm_in_tokens": 2000, "llm_out_tokens": 100}
    # Summe lässt sich aus der Basis nachrechnen
    b = p["basis"]
    recomputed = (b["stt_audio_s"] * live.PRICE_STT_PER_S + b["tts_chars"] * live.PRICE_TTS_PER_CHAR
                  + b["llm_in_tokens"] * live.PRICE_LLM_IN + b["llm_out_tokens"] * live.PRICE_LLM_OUT)
    assert p["usd"] == pytest.approx(recomputed, abs=1e-5)


def test_meter_live_basis_only_realtime_fields():
    meter = live.CostMeter("live")
    meter.add(_rt(audio_in=1000, text_in=200, audio_out=500, text_out=50))
    meter.add(_rt(audio_in=1000, audio_out=500))
    p = meter.payload()
    assert p["basis"] == {"rt_audio_in_tokens": 2000, "rt_audio_out_tokens": 1000,
                          "rt_text_in_tokens": 200, "rt_text_out_tokens": 50}
    assert p["usd"] == pytest.approx(round((2000 * 3 + 200 * 0.75 + 1000 * 12 + 50 * 4.5) / 1e6, 5))


def test_meter_add_returns_cost_of_single_metric():
    meter = live.CostMeter("live")
    assert meter.add(_rt(audio_in=1_000_000)) == pytest.approx(3.00)
    assert meter.add(_m("VADMetrics")) == 0.0
    assert meter.usd == pytest.approx(3.00)


def test_meter_sends_only_on_change():
    meter = live.CostMeter("pipeline")
    assert meter.take_update() is None                                 # ohne Kosten nie eine Meldung
    meter.add(_m("TTSMetrics", characters_count=100))
    first = meter.take_update()
    assert first is not None and first["usd"] == pytest.approx(0.003)   # Positivkontrolle
    assert meter.take_update() is None                                 # nichts Neues
    meter.add(_m("VADMetrics"))
    assert meter.take_update() is None                                 # Stille/unbekannt: keine Meldung
    meter.add(_m("TTSMetrics", characters_count=100))
    second = meter.take_update()
    assert second is not None and second["usd"] == pytest.approx(0.006)


# ── operator.say im Live-Modus: Inhaltstreue, bei Markierung/Transkript wörtlich ──
# Bug 01.10.: "sinngemäß"-Prompt -> Gemini kürzte, deutete um und erfand Behauptungen.

BUG_TEXT = "Laut Transkript: Meine Pflanze steht hinter meinem Grillkraftgel und das ist Spülmittel."


def _quoted(user_input: str) -> str:
    return user_input[user_input.index("«") + 1:user_input.rindex("»")]


def test_live_say_prompt_no_longer_allows_paraphrase():
    # Positivkontrolle gegen den alten Prompt: der erlaubte genau das Fehlverhalten
    assert "sinngemäß" not in live.LIVE_SAY_USER and "kurz" not in live.LIVE_SAY_USER
    for tpl in (live.LIVE_SAY_USER, live.LIVE_SAY_VERBATIM_USER):
        assert tpl.startswith("[Agent]")
        for rule in ("Einleitung", "Nachsatz"):
            assert rule in tpl
    for rule in ("nichts hinzufügen", "keine eigenen Behauptungen", "nichts weglassen", "abschwächen", "umdeuten"):
        assert rule in live.LIVE_SAY_USER
    assert "Wort für Wort" in live.LIVE_SAY_VERBATIM_USER


def test_live_base_instructions_require_content_fidelity():
    # Delta-Kern Live (core.py, DELTA_CORE.md 3b): Inhalt treu, Formulierung frei
    t = live.LIVE_BASE_INSTRUCTIONS.lower()
    for rule in ("vollständig und unverfälscht", "nichts hinzufügen", "keine eigenen fakten",
                 "nichts weglassen", "kein nachsatz", "wort für wort"):
        assert rule in t, rule
    # Vorgaben ([System]) bleiben stumm, Aussagen ([Agent]) werden gesprochen
    assert "nie vorlesen" in t


def test_live_say_plain_text_uses_fidelity_prompt_with_exact_text():
    u = live.live_say_user_input("Der Termin ist am Dienstag um zehn.")
    assert u == live.LIVE_SAY_USER.format(text="Der Termin ist am Dienstag um zehn.")
    assert _quoted(u) == "Der Termin ist am Dienstag um zehn."


def test_live_say_transcript_text_is_verbatim():
    # der Fall aus dem Live-Test 01.10.
    u = live.live_say_user_input(BUG_TEXT)
    assert u.startswith("[Agent] Wörtlich.") and _quoted(u) == BUG_TEXT


@pytest.mark.parametrize("prefix", ["wörtlich: ", "Wörtlich:", "eins zu eins: ", "Eins zu Eins : ", "1:1: ", "1 zu 1: "])
def test_live_say_explicit_verbatim_prefix_is_stripped(prefix):
    u = live.live_say_user_input(prefix + "Ich bin gleich zurück.")
    assert u.startswith("[Agent] Wörtlich.") and _quoted(u) == "Ich bin gleich zurück."


@pytest.mark.parametrize("text", ['Er sagte "morgen".', "Sie schrieb „passt“.", "Zitat von Max: geht klar"])
def test_live_say_quotes_are_verbatim(text):
    assert _quoted(live.live_say_user_input(text)) == text
    assert live.live_say_user_input(text).startswith("[Agent] Wörtlich.")


@pytest.mark.parametrize("text", ["1:1-Kopie liegt bereit.", "wörtlich:", "Das ist wörtlich gemeint."])
def test_live_say_no_false_verbatim_trigger(text):
    # Markierung nur als führendes "X:" mit Rest; sonst Normalprompt, Text unverändert
    u = live.live_say_user_input(text)
    assert u == live.LIVE_SAY_USER.format(text=text)


@pytest.mark.asyncio
async def test_live_say_relay_sends_verbatim_prompt_for_transcript():
    session = MagicMock()
    session.generate_reply = MagicMock(return_value=None)
    h = build_relay_handlers(session, RelayAgent(instructions="x"), live=True)
    await h.on_say(_Pkt(TOPIC_SAY, json.dumps({"text": BUG_TEXT}).encode()))
    session.say.assert_not_called()
    kwargs = session.generate_reply.call_args.kwargs
    assert kwargs["user_input"] == live.live_say_user_input(BUG_TEXT)
    assert kwargs["allow_interruptions"] is True and "instructions" not in kwargs


@pytest.mark.asyncio
async def test_live_say_handle_still_marks_operator_role():
    # Transkript-Rolle "operator" hängt im Live-Modus am Handle, nicht am Text
    session, handle = MagicMock(), MagicMock()
    handle.done.return_value = False
    session.generate_reply = MagicMock(return_value=handle)
    h = build_relay_handlers(session, RelayAgent(instructions="x"), live=True)
    await h.on_say(_Pkt(TOPIC_SAY, json.dumps({"text": "Termin Dienstag"}).encode()))
    assert h.is_operator_speech(handle, "Der Termin ist Dienstag.") is True
    assert h.is_operator_speech(MagicMock(), "Der Termin ist Dienstag.") is False


def test_base_prompts_defer_capability_questions_to_operator():
    """Oliver 01.10.: Stimme verneinte Gmail-Zugriff des Operators. Faehigkeitsfragen -> nachschauen."""
    from agent.relay import DEFAULT_PERSONA

    for t in (live.LIVE_BASE_INSTRUCTIONS, DEFAULT_PERSONA):
        assert "Ich frag deinen Agenten kurz." in t
        assert "Zugriff hast" in t and "beantwortest du nie selbst" in t
