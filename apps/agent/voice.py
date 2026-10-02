"""STT + TTS factories — Deepgram Nova-3 + Google Chirp3-HD.

Provider choices are baked in (no fallback chain — every additional path is a
silent failure mode, voicehook-v3#62/#61). Defaults match what the v3 box
actually called in the last 7d window per the inventory: Deepgram Nova-3 for
STT, Google Chirp3-HD-Charon for TTS. Both honor language + voice env overrides.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from livekit.plugins.deepgram import STT as DeepgramSTT
    from livekit.plugins.google import TTS as GoogleTTS

log = logging.getLogger("voicehook.voice")

# Defaults from voicehook-v3 inventory (2026-05-29, 7d log evidence).
DEFAULT_STT_MODEL = "nova-3"
DEFAULT_LANGUAGE = "de"
DEFAULT_TTS_VOICE = "de-DE-Chirp3-HD-Charon"


def build_stt(
    *,
    model: str | None = None,
    language: str | None = None,
    diarize: bool | None = None,
) -> DeepgramSTT:
    """Deepgram Nova-3 STT. Requires DEEPGRAM_API_KEY in the env.

    `diarize` (Default: Schalter VOICEHOOK_STT_DIARIZE, an) setzt im Plugin
    `enable_diarization` -> Deepgram-Live-Parameter `diarize=true`; finale
    Transkripte tragen dann `speaker_id` ("S0", "S1", ...) für den
    Hauptsprecher-Filter (agent/speaker.py).
    """
    from livekit.plugins.deepgram import STT

    from .speaker import diarize_enabled

    return STT(
        model=model or os.environ.get("VOICEHOOK_STT_MODEL", DEFAULT_STT_MODEL),
        language=language or os.environ.get("VOICEHOOK_LANGUAGE", DEFAULT_LANGUAGE),
        enable_diarization=diarize_enabled() if diarize is None else diarize,
    )


def build_tts(*, voice: str | None = None, language: str | None = None) -> GoogleTTS:
    """Google Chirp3-HD TTS. Requires GOOGLE_APPLICATION_CREDENTIALS (GCP SA JSON path).

    `language` must match the voice's language prefix (Google rejects e.g.
    voice=de-DE-Chirp3-HD-Charon with language=en-US → 400 "code to match"). We
    auto-derive the BCP-47 lang from the voice name's prefix when not given.
    """
    from livekit.plugins.google import TTS

    v = voice or os.environ.get("VOICEHOOK_TTS_VOICE", DEFAULT_TTS_VOICE)
    # voice name is "de-DE-Chirp3-HD-Charon" — first two dash-segments = lang code
    auto_lang = "-".join(v.split("-")[:2]) if "-" in v else "en-US"
    lang = language or os.environ.get("VOICEHOOK_TTS_LANGUAGE", auto_lang)
    return TTS(voice_name=v, language=lang)


# ---------------------------------------------------------------------------
# Vorwärmen (Prod-Log 02.10.: erster operator.say verzerrt, genau dort
# "event loop blocked for 107ms in grpc/aio/_channel.py __init__"; beim Start
# blockierte der Import von google.genai.types 1440 ms).
#
# Das Plugin (livekit-plugins-google 1.8.3, tts.py `_ensure_client`) baut den
# TextToSpeechAsyncClient samt gRPC-aio-Kanal erst beim ersten synthesize/stream,
# also synchron in der Event-Loop mitten im ersten Satz; danach wird er pro
# TTS-Instanz wiederverwendet. Ein `prewarm()` überschreibt es nicht (Basis = no-op).
# Darum: Kanal beim Job-Start selbst anlegen (gleiche Loop, gleiche Instanz) und
# ihn mit ListVoices verbinden. ListVoices synthetisiert nichts, ist nicht
# abrechenbar (Cloud TTS rechnet nur synthetisierte Zeichen) und ist unhörbar.
# ---------------------------------------------------------------------------

TTS_WARM_TIMEOUT_S = 5.0
_warm_tasks: set[asyncio.Task] = set()  # starke Referenz, sonst kann der GC den Task einsammeln


def preload_modules() -> None:
    """prewarm_fnc des Job-Prozesses: schwere Google-Importe vor dem ersten Job laden.

    Läuft im Leerlauf-Prozess (num_idle_processes), nicht in der Event-Loop eines Calls.
    """
    import google.genai.types  # noqa: F401  (Gemini-LLM/Live, Zusammenfassung)
    import livekit.plugins.google  # noqa: F401  (zieht google.cloud.texttospeech + grpc)


def warm_tts(tts: Any) -> asyncio.Task | None:
    """Google-TTS-Client + gRPC-Kanal jetzt anlegen und im Hintergrund verbinden.

    Aufruf beim Job-Start in der Job-Event-Loop, bevor jemand etwas hört. Der
    Kanalaufbau (~100 ms synchron) fällt damit nicht mehr in den ersten Satz. Der
    zurückgegebene Task verbindet per ListVoices (TLS, HTTP/2, OAuth-Token), kostenlos
    und ohne Ton. Fehler sind nie fatal: dann baut das Plugin wie bisher beim ersten Satz.
    """
    ensure = getattr(tts, "_ensure_client", None)
    if ensure is None:
        return None
    try:
        client = ensure()
    except Exception as e:  # noqa: BLE001
        log.warning("[tts-warm] client: %s", e)
        return None
    lang = getattr(getattr(getattr(tts, "_opts", None), "voice", None), "language_code", None)

    async def _connect() -> bool:
        t0 = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(
                client.list_voices(language_code=lang or DEFAULT_LANGUAGE, timeout=TTS_WARM_TIMEOUT_S),
                TTS_WARM_TIMEOUT_S + 1,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("[tts-warm] connect: %s", e)
            return False
        log.info("[tts-warm] channel ready in %.0f ms", (asyncio.get_running_loop().time() - t0) * 1000)
        return True

    try:
        task = asyncio.get_running_loop().create_task(_connect())
    except RuntimeError:  # keine laufende Loop (Tests)
        return None
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)
    return task
