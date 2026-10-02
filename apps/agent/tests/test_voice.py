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


def test_build_stt_diarize_default_an(monkeypatch):
    monkeypatch.delenv("VOICEHOOK_STT_DIARIZE", raising=False)
    stt = build_stt()
    assert stt._opts.enable_diarization is True
    assert stt.capabilities.diarization is True


def test_build_stt_diarize_schalter_aus(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STT_DIARIZE", "0")
    assert build_stt()._opts.enable_diarization is False
    # explizites Argument schlägt den Schalter
    assert build_stt(diarize=True)._opts.enable_diarization is True


# --- Vorwärmen (Prod 02.10.: 107 ms gRPC-Kanalaufbau im ersten Satz) -------------


def test_preload_modules_laedt_google_vor():
    import sys

    from agent.voice import preload_modules

    preload_modules()
    assert "google.genai.types" in sys.modules
    assert "livekit.plugins.google" in sys.modules


class _FakeClient:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def list_voices(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("401")
        return object()


class _FakeTTS:
    def __init__(self, client=None, ensure_error=None):
        self.client = client or _FakeClient()
        self.ensure_error = ensure_error
        self.ensured = 0

    def _ensure_client(self):
        self.ensured += 1
        if self.ensure_error:
            raise self.ensure_error
        return self.client

    def synthesize(self, *a, **kw):  # darf beim Vorwärmen nie aufgerufen werden (Kosten, Ton)
        raise AssertionError("warm_tts darf nicht synthetisieren")

    stream = synthesize


async def test_warm_tts_baut_client_sofort_und_verbindet_ohne_synthese():
    from agent.voice import warm_tts

    tts = _FakeTTS()
    task = warm_tts(tts)
    assert tts.ensured == 1  # synchron beim Job-Start, nicht erst im ersten Satz
    assert await task is True
    assert tts.client.calls == [{"language_code": "de", "timeout": 5.0}]


async def test_warm_tts_nutzt_sprache_der_stimme():
    from agent.voice import build_tts, warm_tts

    real = build_tts(voice="en-US-Chirp3-HD-Kore")
    fake = _FakeTTS()
    fake._opts = real._opts
    assert await warm_tts(fake) is True
    assert fake.client.calls[0]["language_code"] == "en-US"


async def test_warm_tts_fehler_sind_nie_fatal():
    from agent.voice import warm_tts

    assert await warm_tts(_FakeTTS(client=_FakeClient(fail=True))) is False
    assert warm_tts(_FakeTTS(ensure_error=RuntimeError("no creds"))) is None
    assert warm_tts(None) is None
    assert warm_tts(object()) is None


def test_warm_tts_ohne_laufende_loop_kein_absturz():
    from agent.voice import warm_tts

    tts = _FakeTTS()
    assert warm_tts(tts) is None
    assert tts.ensured == 1
