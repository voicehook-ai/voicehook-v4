"""Antwortzeit pro Nutzer-Turn zerlegen: eine Logzeile `[timing]` je Turn (Oliver 02.10.).

Frage: wo stecken die ~3 s zwischen Ende des Nutzersatzes und dem ersten Ton von Delta?
Quelle sind die vorhandenen livekit-Metriken (`metrics_collected`), verbunden über die
speech_id der Antwort; ergänzt wird nur der Wiedergabebeginn (agent_state -> speaking).

Alle Zeiten in ms relativ zum VAD-Ende der Nutzersprache (t=0):
  stt      finales Transkript da          (EOUMetrics.transcription_delay)
  eot      Turn-Ende entschieden          (EOUMetrics.end_of_utterance_delay)
  otc      on_user_turn_completed fertig  (+ on_user_turn_completed_delay)
  llm0     LLM-Anfrage gestartet          (LLMMetrics.timestamp - duration; negativ = preemptive)
  ttft     erstes LLM-Token               (llm0 + LLMMetrics.ttft)
  llm1     LLM fertig (oder Stream gekappt)
  tts      erster TTS-Audioframe          (TTS-Start + TTSMetrics.ttfb, Start = erster Text)
  play     Wiedergabe beginnt             (agent_state_changed -> speaking)
t0=vad: Nullpunkt VAD-Ende; t0=eot: livekit kannte kein VAD-Ende (Delays 0), Nullpunkt = Turn-Ende.
Turns ohne Wiedergabe (abgebrochen/abgelöst) werden beim nächsten Turn mit play=- geloggt.
Kein Text, keine Namen: nur Zeiten und die speech_id als Turn-ID.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("voicehook.timing")

FIELDS = ("stt", "eot", "otc", "llm0", "ttft", "llm1", "tts", "play")


@dataclass
class _Turn:
    sid: str
    vad_end: float  # Wanduhr (time.time), Sekunden
    anchor: str = "vad"  # "eot": VAD-Ende unbekannt (livekit meldet 0), t=0 ist dann das Turn-Ende
    t: dict[str, float] = field(default_factory=dict)  # Feld -> Wanduhr
    logged: bool = False


class TurnTiming:
    """Sammelt die Metriken eines Turns (Schlüssel speech_id) und loggt einmal je Turn."""

    def __init__(self, *, log: logging.Logger | None = None, max_open: int = 32) -> None:
        self._log = log or logger
        self._turns: dict[str, _Turn] = {}
        self._max_open = max_open

    # -- Eingänge -------------------------------------------------------------

    def on_metrics(self, m: Any) -> None:
        kind = type(m).__name__
        sid = getattr(m, "speech_id", None)
        if not sid:
            return
        ts = float(getattr(m, "timestamp", 0.0) or 0.0)
        if kind == "EOUMetrics":
            eou = float(getattr(m, "end_of_utterance_delay", 0.0) or 0.0)
            otc = float(getattr(m, "on_user_turn_completed_delay", 0.0) or 0.0)
            stt = float(getattr(m, "transcription_delay", 0.0) or 0.0)
            decided = ts - otc
            vad_end = decided - eou
            # Ein neuer Turn löst ältere ab, die nie abgespielt wurden (abgebrochen, verworfen)
            for old in [k for k, v in self._turns.items() if "play" not in v.t]:
                self.finish(old)
            turn = _Turn(sid, vad_end, "vad" if (eou or stt) else "eot")
            turn.t.update(stt=vad_end + stt, eot=decided, otc=ts)
            self._turns[sid] = turn
            while len(self._turns) > self._max_open:  # nie wachsen lassen (Turns ohne Ende)
                self.finish(next(iter(self._turns)))
            return
        turn = self._turns.get(sid)
        if turn is None:
            return
        if kind == "LLMMetrics" and "llm0" not in turn.t:
            start = ts - float(getattr(m, "duration", 0.0) or 0.0)
            turn.t["llm0"] = start
            ttft = float(getattr(m, "ttft", -1.0) or -1.0)
            if ttft >= 0:
                turn.t["ttft"] = start + ttft
            turn.t["llm1"] = ts
        elif kind == "TTSMetrics" and "tts" not in turn.t:
            ttfb = float(getattr(m, "ttfb", -1.0) or -1.0)
            if ttfb >= 0:
                turn.t["tts"] = ts - float(getattr(m, "duration", 0.0) or 0.0) + ttfb

    def on_playout(self, sid: str | None, at: float) -> None:
        turn = self._turns.get(sid or "")
        if turn is not None and "play" not in turn.t:
            turn.t["play"] = at

    def finish(self, sid: str | None) -> str | None:
        """Antwort fertig (oder abgebrochen): Zeile loggen, Turn vergessen."""
        turn = self._turns.pop(sid or "", None)
        if turn is None or turn.logged:
            return None
        turn.logged = True
        line = format_line(turn.sid, turn.vad_end, turn.t, turn.anchor)
        self._log.info(line)
        return line


def format_line(sid: str, vad_end: float, t: dict[str, float], anchor: str = "vad") -> str:
    parts = [f"turn={sid}", f"t0={anchor}"]
    for k in FIELDS:
        v = t.get(k)
        parts.append(f"{k}={round((v - vad_end) * 1000)}" if v is not None else f"{k}=-")
    return "[timing] " + " ".join(parts)


def attach(session: Any, timing: TurnTiming | None = None) -> TurnTiming:
    """An eine AgentSession hängen (Normal-Pipeline). Eigene Listener, ändert nichts am Ablauf."""
    timing = timing or TurnTiming()

    @session.on("metrics_collected")
    def _m(ev) -> None:  # noqa: ANN001
        try:
            timing.on_metrics(getattr(ev, "metrics", None))
        except Exception as e:  # noqa: BLE001
            logger.debug("[timing] metrics: %s", e)

    @session.on("agent_state_changed")
    def _s(ev) -> None:  # noqa: ANN001
        if getattr(ev, "new_state", None) != "speaking":
            return
        try:
            h = session.current_speech
            sid = str(getattr(h, "id", "") or "")
            timing.on_playout(sid, float(getattr(ev, "created_at", 0.0)))
            if sid and h is not None:
                h.add_done_callback(lambda _h, _sid=sid: timing.finish(_sid))
        except Exception as e:  # noqa: BLE001
            logger.debug("[timing] playout: %s", e)

    return timing
