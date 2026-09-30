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


def test_build_live_llm_defaults(monkeypatch):
    captured = {}
    monkeypatch.setattr(live, "_realtime_model_cls", lambda: (lambda **kw: captured.update(kw) or "MODEL"))
    for k in ("VOICEHOOK_LIVE_MODEL", "VOICEHOOK_LIVE_VOICE", "VOICEHOOK_LIVE_TRIGGER_TOKENS", "VOICEHOOK_LIVE_TARGET_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    assert live.build_live_llm() == "MODEL"
    assert captured["model"] == "gemini-3.8-live"
    assert "language" not in captured              # native audio ignoriert language_code
    cwc = captured["context_window_compression"]
    assert cwc.trigger_tokens == 12000 and cwc.sliding_window.target_tokens == 6000
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
    assert kwargs["user_input"].startswith("[Operator]") and "Termin ist Dienstag" in kwargs["user_input"]
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
    assert last.role == "user" and "[Operator]" in last.text_content and "Du bist Coach" in last.text_content


def test_live_base_instructions_pin_voice_and_language():
    t = live.LIVE_BASE_INSTRUCTIONS
    assert "Deutsch" in t and "Stimme" in t and "[Operator]" in t


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
