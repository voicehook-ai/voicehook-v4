"""Hauptsprecher-Filter: nur die Stimme des Hauptsprechers erreicht das LLM."""

from __future__ import annotations

import logging

import pytest
from livekit.agents import Agent, stt
from livekit.plugins.deepgram.stt import live_transcription_to_speech_data

from agent.relay import RelayAgent
from agent.speaker import DEFAULT_MIN_PRIMARY_S, PrimarySpeakerFilter, diarize_enabled


def _deepgram_final(words: list[tuple[str, float, float, int | None]]) -> dict:
    """Deepgram-Live-Antwort (Results) mit Wörtern (text, start, end, speaker)."""
    ws = []
    for text, start, end, spk in words:
        w = {"word": text, "punctuated_word": text, "start": start, "end": end, "confidence": 0.9}
        if spk is not None:
            w["speaker"] = spk
        ws.append(w)
    return {"channel": {"alternatives": [{"transcript": " ".join(w[0] for w in words), "confidence": 0.9, "words": ws}]}}


def _final(words: list[tuple[str, float, float, int | None]]) -> stt.SpeechEvent:
    """Finales Ereignis, gebaut mit der echten Plugin-Umwandlung (kein erfundenes Format)."""
    alts = live_transcription_to_speech_data("de", _deepgram_final(words), is_final=True, start_time_offset=0.0)
    return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id="r", alternatives=alts)


def _interim(text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
        alternatives=[stt.SpeechData(language="de", text=text)],
    )


async def _run(filt: PrimarySpeakerFilter, events: list[stt.SpeechEvent]) -> list[stt.SpeechEvent]:
    async def src():
        for e in events:
            yield e

    return [e async for e in filt.filter(src())]


def _final_texts(out: list[stt.SpeechEvent]) -> list[str]:
    return [
        e.alternatives[0].text
        for e in out
        if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT and e.alternatives[0].text
    ]


OLIVER = [("Hallo", 0.0, 0.5, 0), ("ich", 0.5, 0.8, 0), ("bin", 0.8, 1.2, 0), ("Oliver", 1.2, 4.0, 0)]
NACHBAR = [("Fick", 5.0, 5.3, 1), ("deine", 5.3, 5.6, 1), ("Eltern", 5.6, 6.0, 1)]


def test_plugin_liefert_speaker_id_nur_final():
    """Beleg aus dem installierten Plugin: final S<n>, interim None."""
    data = _deepgram_final(OLIVER)
    final = live_transcription_to_speech_data("de", data, is_final=True, start_time_offset=0.0)
    interim = live_transcription_to_speech_data("de", data, is_final=False, start_time_offset=0.0)
    assert final[0].speaker_id == "S0"
    assert interim[0].speaker_id is None


def test_diarize_schalter_default_an(monkeypatch):
    monkeypatch.delenv("VOICEHOOK_STT_DIARIZE", raising=False)
    assert diarize_enabled() is True
    monkeypatch.setenv("VOICEHOOK_STT_DIARIZE", "0")
    assert diarize_enabled() is False
    monkeypatch.setenv("VOICEHOOK_STT_DIARIZE", "off")
    assert diarize_enabled() is False


def test_min_s_env_override(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_SPEAKER_MIN_S", "7.5")
    assert PrimarySpeakerFilter().min_primary_s == 7.5
    monkeypatch.setenv("VOICEHOOK_SPEAKER_MIN_S", "quatsch")
    assert PrimarySpeakerFilter().min_primary_s == DEFAULT_MIN_PRIMARY_S


@pytest.mark.asyncio
async def test_nachbar_wird_verworfen_hauptsprecher_kommt_durch():
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    out = await _run(filt, [_final(OLIVER), _interim("Fick deine"), _final(NACHBAR), _final(OLIVER)])
    texts = _final_texts(out)
    # Positivkontrolle: Oliver kommt beide Male durch
    assert texts == ["Hallo ich bin Oliver", "Hallo ich bin Oliver"]
    assert "Fick deine Eltern" not in " ".join(texts)
    assert filt.primary == "S0"
    assert filt.dropped["S1"].words == 3
    assert filt.dropped["S1"].seconds == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_verworfenes_segment_loescht_zwischenergebnis():
    """Statt des Finals: leeres INTERIM (löscht das Zwischenergebnis) + leeres FINAL."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    out = await _run(filt, [_final(OLIVER), _final(NACHBAR)])
    tail = out[-2:]
    assert [e.type for e in tail] == [stt.SpeechEventType.INTERIM_TRANSCRIPT, stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert all(e.alternatives[0].text == "" for e in tail)


@pytest.mark.asyncio
async def test_einlernphase_laesst_alles_durch():
    """Unter min_primary_s ist noch kein Hauptsprecher sicher: alles geht durch."""
    filt = PrimarySpeakerFilter(min_primary_s=10.0)
    out = await _run(filt, [_final(OLIVER), _final(NACHBAR)])
    assert _final_texts(out) == ["Hallo ich bin Oliver", "Fick deine Eltern"]
    assert filt.dropped == {}


@pytest.mark.asyncio
async def test_hauptsprecher_wechselt_bei_mehr_sprechzeit():
    """Hauptsprecher = meiste bestätigte Sprechzeit, auch wenn er nicht zuerst spricht."""
    filt = PrimarySpeakerFilter(min_primary_s=0.0)
    kurz = [("Hallo", 0.0, 0.5, 1)]
    lang = [("ich", 1.0, 2.0, 0), ("rede", 2.0, 3.0, 0), ("lange", 3.0, 4.0, 0)]
    out = await _run(filt, [_final(kurz), _final(lang), _final(kurz)])
    assert _final_texts(out) == ["Hallo", "ich rede lange"]
    assert filt.primary == "S0"


@pytest.mark.asyncio
async def test_ohne_sprecher_info_fail_open_und_einmal_geloggt(caplog):
    filt = PrimarySpeakerFilter(min_primary_s=0.0)
    ohne = [("was", 0.0, 1.0, None), ("geht", 1.0, 2.0, None)]
    with caplog.at_level(logging.WARNING, logger="voicehook.speaker"):
        out = await _run(filt, [_final(ohne), _final(ohne), _final(ohne)])
    assert _final_texts(out) == ["was geht"] * 3
    warns = [r for r in caplog.records if "keine Sprecher-Info" in r.getMessage()]
    assert len(warns) == 1


@pytest.mark.asyncio
async def test_andere_ereignisse_unveraendert():
    filt = PrimarySpeakerFilter(min_primary_s=0.0)
    evs = [
        stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH),
        _interim("Hallo"),
        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH),
    ]
    out = await _run(filt, evs)
    assert out == evs


@pytest.mark.asyncio
async def test_log_pro_call_nennt_verworfene_sekunden_und_woerter(caplog):
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    with caplog.at_level(logging.INFO, logger="voicehook.speaker"):
        await _run(filt, [_final(OLIVER), _final(NACHBAR)])
    msgs = [r.getMessage() for r in caplog.records]
    assert any("primary=S0 verworfen: S1 1.0s/3 Wörter" in m for m in msgs)


# --- Verdrahtung im RelayAgent -------------------------------------------------


def test_relay_agent_hat_filter_per_default(monkeypatch):
    monkeypatch.delenv("VOICEHOOK_STT_DIARIZE", raising=False)
    assert isinstance(RelayAgent(instructions="x").speakers, PrimarySpeakerFilter)
    monkeypatch.setenv("VOICEHOOK_STT_DIARIZE", "0")
    assert RelayAgent(instructions="x").speakers is None


@pytest.mark.asyncio
async def test_relay_stt_node_filtert_default_ausgabe(monkeypatch):
    seen_audio = []

    async def fake_default(agent, audio, model_settings):
        async for f in audio:
            seen_audio.append(f)
        for e in [_final(OLIVER), _final(NACHBAR)]:
            yield e

    monkeypatch.setattr(Agent.default, "stt_node", staticmethod(fake_default))

    class Gate:
        async def filter(self, audio):
            async for f in audio:
                yield f + "-gated"

    async def audio():
        yield "frame"

    agent = RelayAgent(instructions="x", gate=Gate(), speakers=PrimarySpeakerFilter(min_primary_s=3.0))
    out = [e async for e in agent.stt_node(audio(), None)]
    assert seen_audio == ["frame-gated"]  # Audio-Gate bleibt erhalten
    assert _final_texts(out) == ["Hallo ich bin Oliver"]


@pytest.mark.asyncio
async def test_relay_stt_node_ohne_filter_unveraendert(monkeypatch):
    """Positivkontrolle: ohne Filter kommt der Nachbar durch (der Filter macht den Unterschied)."""

    async def fake_default(agent, audio, model_settings):
        for e in [_final(OLIVER), _final(NACHBAR)]:
            yield e

    monkeypatch.setattr(Agent.default, "stt_node", staticmethod(fake_default))

    async def audio():
        yield "frame"

    agent = RelayAgent(instructions="x", speakers=None)
    out = [e async for e in agent.stt_node(audio(), None)]
    assert _final_texts(out) == ["Hallo ich bin Oliver", "Fick deine Eltern"]
