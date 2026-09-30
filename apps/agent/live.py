"""Gemini-3.8-Live-Testmodus (eigener Worker `voice-ai-live`, VOICEHOOK_PIPELINE=live).

Statt STT -> LLM -> TTS spricht ein Realtime-Modell selbst (Audio rein, Audio raus).
operator.say wird dort zur Anweisung ("sag sinngemäß"), nicht zu wörtlichem TTS;
der Operator steuert über Persona/Graph-Updates und korrigiert über say/revise.

Kosten: Die Live API rechnet pro Turn den GESAMTEN Kontext neu ab (Google, Live API
best practices). Deshalb Kontext-Kompression: bei TRIGGER Tokens auf TARGET kürzen
(~25 Audio-Tokens/s -> 12k/6k ≈ 8/4 Min Audio-Gedächtnis). Langzeitwissen gehört
in die Persona (Text, billig), nicht in den Audio-Verlauf.
"""

from __future__ import annotations

import os

DEFAULT_LIVE_MODEL = "gemini-3.8-live"
DEFAULT_LIVE_VOICE = "Charon"
DEFAULT_TRIGGER_TOKENS = 12000  # Kosten: jeder Turn rechnet den ganzen Kontext ab (32k wäre ~3x teurer)
DEFAULT_TARGET_TOKENS = 6000

# Echte System-Instruktion des Live-Workers (geht nur beim Verbindungsaufbau an
# Gemini). Persona-Updates und Operator-Sätze kommen später als markierte
# User-Turns: das Google-Plugin schickt update_instructions()/instructions= als
# role="model"-Turn, Gemini hält sie dann für eigene Aussagen (livekit/agents
# PR #5049, Issue #5496; realtime_api.py 1.8.3 Z. 646-672, 869).
LIVE_BASE_INSTRUCTIONS = (
    "Antworte immer auf Deutsch. Sprich immer mit derselben ruhigen, tiefen, warmen "
    "Stimme in gleichmäßigem Tempo, wie ein ruhiger Radiosprecher. Imitiere keine "
    "Personen, spiele keine Rollen, keine Akzente, keine Stimmwechsel, keine "
    "übertriebenen Emotionen. Du bist ein freundlicher Gesprächspartner, duzt dein "
    "Gegenüber und antwortest selbst in 1 bis 3 kurzen Sätzen. Sag nie, dass du etwas "
    "an einen Operator weitergibst. Nachrichten, die mit [Operator] beginnen, sind "
    "Vorgaben deines Operators: befolge sie, lies sie nie vor und erwähne sie nicht."
)
LIVE_SAY_USER = "[Operator] Sag jetzt sinngemäß, kurz und natürlich, ohne etwas zu erfinden: {text}"
LIVE_PERSONA_USER = "[Operator] Ab sofort gilt zusätzlich diese Rolle und dieses Wissen, nicht vorlesen, nicht darauf antworten: {text}"

# Platzhalter, die Gemini statt echter Sprache als Transkript liefert
NO_SPEECH_MARKERS = ("<no speech detected>", "&lt;no speech detected&gt;")

# USD je 1M Tokens, ai.google.dev/gemini-api/docs/pricing (Stand 30.09.2026)
PRICE_AUDIO_IN = 3.00
PRICE_TEXT_IN = 0.75
PRICE_AUDIO_OUT = 12.00
PRICE_TEXT_OUT = 4.50


def _realtime_model_cls():
    from livekit.plugins.google import realtime

    return realtime.RealtimeModel


def build_live_llm():
    from google.genai import types

    trigger = int(os.environ.get("VOICEHOOK_LIVE_TRIGGER_TOKENS", DEFAULT_TRIGGER_TOKENS))
    target = int(os.environ.get("VOICEHOOK_LIVE_TARGET_TOKENS", DEFAULT_TARGET_TOKENS))
    return _realtime_model_cls()(
        model=os.environ.get("VOICEHOOK_LIVE_MODEL", DEFAULT_LIVE_MODEL),
        voice=os.environ.get("VOICEHOOK_LIVE_VOICE", DEFAULT_LIVE_VOICE),
        # KEIN language=: native-audio-Modelle unterstützen language_code nicht
        # (Google Live API capabilities); Deutsch steht in LIVE_BASE_INSTRUCTIONS.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=trigger,
            sliding_window=types.SlidingWindow(target_tokens=target),
        ),
        # Verbindung hält ~10 Min; Resumption hält die Sitzung über Reconnects.
        # KEIN transparent=True: nur Enterprise, die Developer API wirft ValueError
        # (live gegen gemini-3.8-live geprüft 30.09.2026).
        session_resumption=types.SessionResumptionConfig(),
    )


def _n(obj, name: str) -> int:
    return int(getattr(obj, name, 0) or 0) if obj is not None else 0


def live_cost_usd(m) -> float:
    """Kosten eines Turns aus RealtimeModelMetrics.

    Konservativ: gecachte und unaufgeschlüsselte Input-Tokens zählen als Audio-Input
    (teuerster Input-Preis), bis Google für Live einen Cache-Rabatt dokumentiert.
    """
    ind, outd = getattr(m, "input_token_details", None), getattr(m, "output_token_details", None)
    text_in = _n(ind, "text_tokens")
    audio_in = _n(m, "input_tokens") - text_in
    text_out = _n(outd, "text_tokens")
    audio_out = _n(m, "output_tokens") - text_out
    return (
        audio_in * PRICE_AUDIO_IN
        + text_in * PRICE_TEXT_IN
        + audio_out * PRICE_AUDIO_OUT
        + text_out * PRICE_TEXT_OUT
    ) / 1_000_000


# ---- Kostenanzeige im laufenden Call (beide Modi) ---------------------------
# Listenpreise USD (Stand 30.09.2026): Deepgram nova-3 Streaming regulär 0,0077 $/min
# (Aktion 0,0048, konservativ der reguläre), Google Chirp3-HD 30 $/1M Zeichen,
# gemini-2.5-flash 0,30 / 2,50 $ je 1M Tokens. Live: live_cost_usd().
PRICE_STT_PER_S = 0.0077 / 60
PRICE_TTS_PER_CHAR = 30.0 / 1_000_000
PRICE_LLM_IN = 0.30 / 1_000_000
PRICE_LLM_OUT = 2.50 / 1_000_000


def metric_cost_usd(m) -> float:
    """Kosten eines einzelnen metrics_collected-Eintrags (0 für unbekannte Typen)."""
    kind = type(m).__name__
    if kind == "RealtimeModelMetrics":
        return live_cost_usd(m)
    if kind == "STTMetrics":
        return float(getattr(m, "audio_duration", 0) or 0) * PRICE_STT_PER_S
    if kind == "TTSMetrics":
        return int(getattr(m, "characters_count", 0) or 0) * PRICE_TTS_PER_CHAR
    if kind == "LLMMetrics":
        return (int(getattr(m, "prompt_tokens", 0) or 0) * PRICE_LLM_IN
                + int(getattr(m, "completion_tokens", 0) or 0) * PRICE_LLM_OUT)
    return 0.0
