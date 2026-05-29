"""STT + TTS factories — Deepgram Nova-3 + Google Chirp3-HD.

Provider choices are baked in (no fallback chain — every additional path is a
silent failure mode, voicehook-v3#62/#61). Defaults match what the v3 box
actually called in the last 7d window per the inventory: Deepgram Nova-3 for
STT, Google Chirp3-HD-Charon for TTS. Both honor language + voice env overrides.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.plugins.deepgram import STT as DeepgramSTT
    from livekit.plugins.google import TTS as GoogleTTS

# Defaults from voicehook-v3 inventory (2026-05-29, 7d log evidence).
DEFAULT_STT_MODEL = "nova-3"
DEFAULT_LANGUAGE = "de"
DEFAULT_TTS_VOICE = "de-DE-Chirp3-HD-Charon"


def build_stt(
    *,
    model: str | None = None,
    language: str | None = None,
) -> DeepgramSTT:
    """Deepgram Nova-3 STT. Requires DEEPGRAM_API_KEY in the env."""
    from livekit.plugins.deepgram import STT

    return STT(
        model=model or os.environ.get("VOICEHOOK_STT_MODEL", DEFAULT_STT_MODEL),
        language=language or os.environ.get("VOICEHOOK_LANGUAGE", DEFAULT_LANGUAGE),
    )


def build_tts(*, voice: str | None = None) -> GoogleTTS:
    """Google Chirp3-HD TTS. Requires GOOGLE_APPLICATION_CREDENTIALS (GCP SA JSON path)."""
    from livekit.plugins.google import TTS

    return TTS(voice_name=voice or os.environ.get("VOICEHOOK_TTS_VOICE", DEFAULT_TTS_VOICE))
