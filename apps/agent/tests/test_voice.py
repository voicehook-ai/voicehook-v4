"""STT/TTS factory tests — verify config wiring without calling provider APIs."""

from __future__ import annotations

from agent.voice import DEFAULT_LANGUAGE, DEFAULT_STT_MODEL, DEFAULT_TTS_VOICE, build_stt, build_tts


def test_build_stt_uses_deepgram_nova3_default():
    stt = build_stt()
    cls = type(stt)
    assert cls.__module__.startswith("livekit.plugins.deepgram")
    assert cls.__name__ == "STT"
    assert stt._opts.model == DEFAULT_STT_MODEL == "nova-3"
    assert stt._opts.language == DEFAULT_LANGUAGE == "de"


def test_build_stt_env_override(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STT_MODEL", "nova-2-general")
    monkeypatch.setenv("VOICEHOOK_LANGUAGE", "en")
    stt = build_stt()
    assert stt._opts.model == "nova-2-general"
    assert stt._opts.language == "en"


def test_build_tts_uses_google_chirp_default():
    tts = build_tts()
    cls = type(tts)
    assert cls.__module__.startswith("livekit.plugins.google")
    assert cls.__name__ == "TTS"
    assert tts._opts.voice.name == DEFAULT_TTS_VOICE == "de-DE-Chirp3-HD-Charon"


def test_build_tts_voice_override():
    tts = build_tts(voice="de-DE-Chirp3-HD-Kore")
    assert tts._opts.voice.name == "de-DE-Chirp3-HD-Kore"


def test_defaults_match_v3_inventory():
    """Documents the chosen defaults — guards against accidental change."""
    assert DEFAULT_STT_MODEL == "nova-3"
    assert DEFAULT_LANGUAGE == "de"
    assert DEFAULT_TTS_VOICE == "de-DE-Chirp3-HD-Charon"
