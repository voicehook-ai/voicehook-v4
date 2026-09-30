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


def _final(words: list[tuple[str, float, float, int | None]], request_id: str = "r") -> stt.SpeechEvent:
    """Finales Ereignis, gebaut mit der echten Plugin-Umwandlung (kein erfundenes Format)."""
    alts = live_transcription_to_speech_data("de", _deepgram_final(words), is_final=True, start_time_offset=0.0)
    return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, request_id=request_id, alternatives=alts)


def _seg(text: str, start: float, end: float, spk: int, request_id: str = "r") -> stt.SpeechEvent:
    """Ein finales Segment eines Sprechers (Wörter gleichmäßig über [start, end])."""
    ws = text.split()
    step = (end - start) / len(ws)
    return _final([(w, start + i * step, start + (i + 1) * step, spk) for i, w in enumerate(ws)], request_id)


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
async def test_verworfene_segmente_zaehlen_nicht():
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    await _run(filt, [_final(OLIVER), _final(NACHBAR), _final(NACHBAR)])
    assert "S1" not in filt.heard
    assert filt.dropped["S1"].words == 6


@pytest.mark.asyncio
async def test_fernseher_konstant_im_hintergrund_oliver_bleibt():
    """TV redet dauernd mit (sogar etwas mehr als Oliver), aber nie doppelt so viel."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    evs = [_seg("ich bin Oliver und rede", 0.0, 4.0, 0)]
    t = 4.0
    for _ in range(12):  # ~72 s Call
        evs.append(_seg("Tagesschau Nachrichten Wetter", t, t + 3.0, 1))
        evs.append(_seg("und weiter gehts", t + 3.5, t + 5.5, 0))
        t += 6.0
    out = await _run(filt, evs)
    texts = _final_texts(out)
    assert filt.primary == "S0"
    assert texts.count("und weiter gehts") == 12  # Positivkontrolle: Oliver immer durch
    assert "Tagesschau Nachrichten Wetter" not in texts
    assert filt.dropped["S1"].seconds == pytest.approx(36.0)


@pytest.mark.asyncio
async def test_fernseher_zuerst_dann_oliver_uebernimmt_und_bleibt():
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    tv_start = [_seg("Guten Abend meine Damen und Herren", 0.0, 4.0, 1)]
    oliver = [_seg(f"Oliver redet Satz {i}", 5.0 + 4 * i, 8.0 + 4 * i, 0) for i in range(4)]
    tv_spaeter = [_seg("Werbung", 21.5, 22.5, 1), _seg("mehr Werbung hier", 26.0, 28.0, 1)]
    oliver_spaeter = [_seg("Oliver nochmal", 23.0, 25.0, 0), _seg("Oliver zuletzt", 29.0, 31.0, 0)]
    evs = tv_start + oliver + [tv_spaeter[0], oliver_spaeter[0], tv_spaeter[1], oliver_spaeter[1]]
    out = await _run(filt, evs)
    texts = _final_texts(out)
    assert filt.primary == "S0"
    # TV lief in der Einlernphase durch; Olivers erste zwei Sätze (3s, 6s < 2x4s)
    # wurden verworfen, ab dem dritten (9s >= 8s) ist Oliver Hauptsprecher.
    assert texts[0] == "Guten Abend meine Damen und Herren"
    assert "Oliver redet Satz 0" not in texts and "Oliver redet Satz 1" not in texts
    assert "Oliver redet Satz 2" in texts and "Oliver redet Satz 3" in texts
    assert "Oliver nochmal" in texts and "Oliver zuletzt" in texts
    assert "Werbung" not in texts and "mehr Werbung hier" not in texts


# --- Neue STT-Verbindung: Deepgram nummeriert Sprecher neu -----------------------

OLIVER_S1 = [(w, s, e, 1) for w, s, e, _ in OLIVER]
TV_S0 = [("Tagesschau", 5.0, 5.5, 0), ("heute", 5.5, 6.0, 0)]


@pytest.mark.asyncio
async def test_neue_request_id_setzt_zurueck_labeltausch():
    """Reconnect (neue request_id): Oliver ist jetzt S1, TV S0. Oliver darf nicht verworfen werden."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    evs = [_final(OLIVER, "a"), _final(NACHBAR, "a"), _final(OLIVER_S1, "b"), _final(TV_S0, "b")]
    out = await _run(filt, evs)
    texts = _final_texts(out)
    assert texts == ["Hallo ich bin Oliver", "Hallo ich bin Oliver"]
    assert filt.primary == "S1"
    assert filt.resets == 2  # Stream-Start + Reconnect


@pytest.mark.asyncio
async def test_ohne_reset_waere_oliver_verworfen():
    """Positivkontrolle: gleiche Folge ohne Verbindungswechsel verwirft Oliver (der Reset macht den Unterschied)."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    evs = [_final(OLIVER, "a"), _final(NACHBAR, "a"), _final(OLIVER_S1, "a")]
    out = await _run(filt, evs)
    assert _final_texts(out) == ["Hallo ich bin Oliver"]


@pytest.mark.asyncio
async def test_nach_reset_wieder_einlernphase():
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    evs = [_final(OLIVER, "a"), _final(NACHBAR, "b")]  # nach Reconnect: Nachbar kommt in Einlernphase durch
    out = await _run(filt, evs)
    assert _final_texts(out) == ["Hallo ich bin Oliver", "Fick deine Eltern"]
    assert filt.primary is None


@pytest.mark.asyncio
async def test_filter_neustart_setzt_zurueck():
    """Pump-Neuaufbau: filter() startet neu, auch bei gleicher request_id wird neu gelernt."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    await _run(filt, [_final(OLIVER), _final(OLIVER), _final(NACHBAR)])
    assert filt.primary == "S0"
    out = await _run(filt, [_final(OLIVER_S1), _final(TV_S0)])
    assert _final_texts(out) == ["Hallo ich bin Oliver"]
    assert filt.primary == "S1"


@pytest.mark.asyncio
async def test_usage_ereignis_mit_alter_request_id_setzt_nicht_zurueck():
    """RECOGNITION_USAGE trägt die zuletzt gesehene request_id, zählt nicht als Verbindungswechsel."""
    filt = PrimarySpeakerFilter(min_primary_s=3.0)
    usage = stt.SpeechEvent(
        type=stt.SpeechEventType.RECOGNITION_USAGE, request_id="alt",
        recognition_usage=stt.RecognitionUsage(audio_duration=1.0),
    )
    out = await _run(filt, [_final(OLIVER), usage, _final(NACHBAR)])
    assert _final_texts(out) == ["Hallo ich bin Oliver"]


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
