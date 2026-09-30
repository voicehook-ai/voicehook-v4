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
    assert captured["language"] == "de-DE"
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
    assert "Termin ist Dienstag" in kwargs["instructions"]
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
