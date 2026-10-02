"""[timing]-Zeile je Nutzer-Turn: Teilzeiten aus den livekit-Metriken, nur Zahlen + Turn-ID."""

from __future__ import annotations

import logging

from livekit.agents.metrics import EOUMetrics, LLMMetrics, TTSMetrics

from agent.turntiming import TurnTiming, attach

VAD_END = 1000.0


def _eou(sid: str = "speech_1") -> EOUMetrics:
    # VAD-Ende 1000.0; Transkript +0.2 s; Turn-Ende +0.5 s; on_user_turn_completed 10 ms
    return EOUMetrics(timestamp=VAD_END + 0.51, end_of_utterance_delay=0.5,
                      transcription_delay=0.2, on_user_turn_completed_delay=0.01, speech_id=sid)


def _llm(sid: str = "speech_1", *, start: float = 0.3, ttft: float = 0.9, dur: float = 1.2) -> LLMMetrics:
    return LLMMetrics(label="g", request_id="r", timestamp=VAD_END + start + dur, duration=dur,
                      ttft=ttft, cancelled=False, completion_tokens=5, prompt_tokens=100,
                      prompt_cached_tokens=0, total_tokens=105, tokens_per_second=4.0,
                      speech_id=sid)


def _tts(sid: str = "speech_1") -> TTSMetrics:
    # erster Text bei +1.25 s, erstes Audio 300 ms danach, Segment 2 s lang
    return TTSMetrics(label="t", request_id="r", timestamp=VAD_END + 3.25, ttfb=0.3, duration=2.0,
                      audio_duration=2.0, cancelled=False, characters_count=40, streamed=True,
                      speech_id=sid)


def test_line_has_all_parts_relative_to_vad_end(caplog):
    tt = TurnTiming()
    for m in (_eou(), _llm(), _tts()):
        tt.on_metrics(m)
    tt.on_playout("speech_1", VAD_END + 1.6)
    with caplog.at_level(logging.INFO, logger="voicehook.timing"):
        line = tt.finish("speech_1")
    assert line == ("[timing] turn=speech_1 t0=vad stt=200 eot=500 otc=510 llm0=300 ttft=1200 "
                    "llm1=1500 tts=1550 play=1600")
    assert line in caplog.text
    assert tt.finish("speech_1") is None  # einmal je Turn


def test_preemptive_llm_start_is_negative_and_missing_parts_dash():
    tt = TurnTiming()
    tt.on_metrics(_eou())
    tt.on_metrics(_llm(start=-0.2))
    line = tt.finish("speech_1")
    assert "llm0=-200" in line and "tts=-" in line and "play=-" in line


def test_foreign_speech_and_operator_say_ignored():
    tt = TurnTiming()
    tt.on_metrics(_llm("speech_x"))  # kein Nutzer-Turn (z. B. operator.say)
    tt.on_playout("speech_x", VAD_END)
    assert tt.finish("speech_x") is None
    tt.on_metrics(_eou())
    tt.on_metrics(_llm())
    tt.on_metrics(_llm(start=5.0))  # zweite LLM-Runde desselben Turns überschreibt nicht
    assert "llm0=300 " in tt.finish("speech_1")


def test_unplayed_turn_logged_when_next_turn_starts_and_unknown_vad_anchor():
    tt = TurnTiming()
    tt.on_metrics(_eou("a"))
    tt.on_metrics(_eou("b"))  # a wurde nie abgespielt -> jetzt mit play=- geloggt
    assert tt.finish("a") is None
    tt.on_playout("b", VAD_END + 1.0)
    tt.on_metrics(EOUMetrics(timestamp=VAD_END + 9.0, end_of_utterance_delay=0.0,
                             transcription_delay=0.0, on_user_turn_completed_delay=0.0,
                             speech_id="c"))
    assert tt.finish("c").startswith("[timing] turn=c t0=eot stt=0 eot=0 otc=0 ")
    assert "play=1000" in tt.finish("b")  # abgespielter Turn bleibt bis zu seinem Ende offen


def test_open_turns_bounded_and_logged_on_eviction():
    lines: list[str] = []

    class _L:
        def info(self, s):  # noqa: ANN001
            lines.append(s)

    tt = TurnTiming(log=_L(), max_open=2)
    for i in range(3):
        tt.on_metrics(_eou(f"s{i}"))
        tt.on_playout(f"s{i}", VAD_END + 1)  # abgespielt, Ende nie gemeldet
    assert len(lines) == 1 and lines[0].startswith("[timing] turn=s0 ")


def test_attach_wires_session_events():
    class _H:
        id = "speech_1"

        def __init__(self):
            self.cbs = []

        def add_done_callback(self, cb):  # noqa: ANN001
            self.cbs.append(cb)

    class _Ev:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _S:
        def __init__(self):
            self.h = {}
            self.current_speech = _H()

        def on(self, name):  # noqa: ANN001
            def deco(fn):
                self.h[name] = fn
                return fn
            return deco

    s = _S()
    lines: list[str] = []

    class _L:
        def info(self, x):  # noqa: ANN001
            lines.append(x)

    attach(s, TurnTiming(log=_L()))
    s.h["metrics_collected"](_Ev(metrics=_eou()))
    s.h["agent_state_changed"](_Ev(new_state="speaking", created_at=VAD_END + 1.0))
    s.current_speech.cbs[0](s.current_speech)
    assert lines == ["[timing] turn=speech_1 t0=vad stt=200 eot=500 otc=510 llm0=- ttft=- llm1=- "
                     "tts=- play=1000"]
