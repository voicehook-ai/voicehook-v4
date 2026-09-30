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
DEFAULT_TRIGGER_TOKENS = 12000
DEFAULT_TARGET_TOKENS = 6000

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
        language=os.environ.get("VOICEHOOK_LANGUAGE", "de-DE"),
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
