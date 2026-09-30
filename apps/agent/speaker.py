"""Hauptsprecher-Filter hinter der Spracherkennung (Oliver 30.09.: Serie im Hintergrund
und Nachbar wurden transkribiert und landeten beim LLM).

Deepgram-Streaming mit `diarize=true` (Plugin-Option `enable_diarization`) liefert je
FINAL_TRANSCRIPT den Mehrheitssprecher des Segments als `SpeechData.speaker_id`
("S0", "S1", ...). Zwischenergebnisse (INTERIM) tragen laut Plugin keine
Sprecher-Info (`speaker_id=None`), gefiltert wird daher an den finalen Segmenten.

Einlernphase: bis ein Sprecher `min_primary_s` Sekunden bestätigte Sprechzeit hat, geht
alles durch; der führende Sprecher wird dann Hauptsprecher (festgelegt). Danach werden
finale Segmente anderer Sprecher verworfen, bevor sie das LLM erreichen. Verworfene
Segmente zählen nicht für die Sprechzeit ihres Sprechers. Der Hauptsprecher wechselt
nur, wenn ein anderer Sprecher in den letzten `window_s` Sekunden mindestens
`switch_ratio`-mal so viel finale Sprechzeit hat wie er (Fernseher läuft mit, Oliver
bleibt; Fernseher hat nur zuerst geredet, Oliver übernimmt). Fehlt die Sprecher-Info,
geht das Segment durch (fail-open: der Call wird nie taub), einmal geloggt.

Deepgram nummeriert Sprecher pro WebSocket neu (S0 der alten Verbindung ist nicht S0
der neuen). Der gelernte Zustand wird daher bei jeder neuen STT-Verbindung verworfen:
- am Start von `filter()` (stt_node/Pump-Neuaufbau = neuer Stream),
- wenn sich die `request_id` der Transkripte ändert. Beleg im Plugin
  (livekit-plugins-deepgram stt.py): `request_id` kommt aus `metadata.request_id` jeder
  Results-Nachricht, also je WebSocket; jede neue Verbindung läuft über `_connect_ws`,
  sowohl beim Retry in `RecognizeStream._main_task` (stt error-Event recoverable=True,
  das nur am STT-Objekt hängt, nicht im Ereignisstrom) als auch beim internen
  Reconnect über `_reconnect_event` (update_options, z. B. keyterm).
Nach dem Zurücksetzen beginnt die Einlernphase neu.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("voicehook.speaker")

# Einlernphase: so viel bestätigte Sprechzeit braucht der führende Sprecher, bevor
# gefiltert wird. Überschreibbar per VOICEHOOK_SPEAKER_MIN_S.
DEFAULT_MIN_PRIMARY_S = 3.0
# Wechsel des Hauptsprechers: Herausforderer braucht im Fenster das Vielfache.
DEFAULT_WINDOW_S = 30.0
DEFAULT_SWITCH_RATIO = 2.0

_OFF = ("0", "false", "off", "no", "")


def diarize_enabled() -> bool:
    """Schalter VOICEHOOK_STT_DIARIZE (Default an)."""
    return os.environ.get("VOICEHOOK_STT_DIARIZE", "1").strip().lower() not in _OFF


def _min_primary_s() -> float:
    raw = os.environ.get("VOICEHOOK_SPEAKER_MIN_S", "").strip()
    if not raw:
        return DEFAULT_MIN_PRIMARY_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("[speaker] VOICEHOOK_SPEAKER_MIN_S=%r ungültig, nehme %.1f", raw, DEFAULT_MIN_PRIMARY_S)
        return DEFAULT_MIN_PRIMARY_S


@dataclass
class _Tally:
    seconds: float = 0.0
    words: int = 0


def _duration(data: Any) -> float:
    start = getattr(data, "start_time", 0.0) or 0.0
    end = getattr(data, "end_time", 0.0) or 0.0
    return max(0.0, end - start)


def _word_count(data: Any) -> int:
    words = getattr(data, "words", None)
    if words:
        return len(words)
    return len((getattr(data, "text", "") or "").split())


class PrimarySpeakerFilter:
    """Lässt nur finale Transkripte des Hauptsprechers zum LLM durch."""

    def __init__(
        self,
        *,
        min_primary_s: float | None = None,
        window_s: float = DEFAULT_WINDOW_S,
        switch_ratio: float = DEFAULT_SWITCH_RATIO,
    ) -> None:
        self.min_primary_s = _min_primary_s() if min_primary_s is None else min_primary_s
        self.window_s = window_s
        self.switch_ratio = switch_ratio
        self.dropped: dict[str, _Tally] = {}   # verworfen je Sprecher (Call-Log, über Resets)
        self.resets = 0
        self._missing_logged = False
        self._conn_id: str | None = None
        self._reset_learned()

    def _reset_learned(self) -> None:
        self.heard: dict[str, _Tally] = {}     # durchgelassene Sprechzeit je Sprecher
        self.primary: str | None = None
        self._recent: deque[tuple[float, str, float]] = deque()  # (Ende, Sprecher, Dauer)
        self._now = 0.0

    def reset(self, reason: str) -> None:
        """Neue STT-Verbindung: Sprecher-Nummern gelten nicht mehr, neu einlernen."""
        if self.heard or self.primary is not None:
            logger.info("[speaker] Zurücksetzen (%s), bisher primary=%s", reason, self.primary)
        self.resets += 1
        self._reset_learned()

    def observe_request_id(self, request_id: str | None) -> None:
        """Wechsel der Deepgram-request_id = neue WebSocket-Verbindung."""
        if not request_id:
            return
        if self._conn_id is not None and request_id != self._conn_id:
            self.reset(f"neue STT-Verbindung {self._conn_id} -> {request_id}")
        self._conn_id = request_id

    def _window(self) -> dict[str, float]:
        cutoff = self._now - self.window_s
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        out: dict[str, float] = {}
        for _, sid, dur in self._recent:
            out[sid] = out.get(sid, 0.0) + dur
        return out

    def _maybe_switch(self) -> None:
        win = self._window()
        cur = win.get(self.primary, 0.0)
        best, best_s = None, 0.0
        for sid, secs in win.items():
            if sid != self.primary and secs > best_s:
                best, best_s = sid, secs
        if best is not None and best_s >= self.min_primary_s and best_s >= self.switch_ratio * cur:
            logger.info(
                "[speaker] Hauptsprecher %s -> %s (%.1fs gegen %.1fs in %.0fs)",
                self.primary, best, best_s, cur, self.window_s,
            )
            self.primary = best

    def accept(self, data: Any) -> bool:
        """Zählt ein finales Segment und entscheidet, ob es zum LLM darf."""
        sid = getattr(data, "speaker_id", None)
        if not sid:
            if not self._missing_logged:
                self._missing_logged = True
                logger.warning("[speaker] keine Sprecher-Info im Transkript, lasse alles durch")
            return True
        dur, words = _duration(data), _word_count(data)
        end = getattr(data, "end_time", 0.0) or 0.0
        self._now = max(self._now, end)
        self._recent.append((end, sid, dur))

        if self.primary is None:
            # Einlernphase: alles geht durch und zählt; der Führende wird festgelegt,
            # sobald er genug Sprechzeit hat.
            tally = self.heard.setdefault(sid, _Tally())
            tally.seconds += dur
            tally.words += words
            lead = max(self.heard, key=lambda k: (self.heard[k].seconds, self.heard[k].words))
            if self.heard[lead].seconds >= self.min_primary_s:
                self.primary = lead
                logger.info("[speaker] Hauptsprecher festgelegt: %s", lead)
            return True

        if sid != self.primary:
            self._maybe_switch()
        if sid == self.primary:
            tally = self.heard.setdefault(sid, _Tally())
            tally.seconds += dur
            tally.words += words
            return True
        d = self.dropped.setdefault(sid, _Tally())
        d.seconds += dur
        d.words += words
        logger.info("[speaker] verwerfe %s (Hauptsprecher %s): %d Wörter", sid, self.primary, words)
        return False

    def summary(self) -> str:
        if not self.dropped:
            return f"primary={self.primary} verworfen=nichts"
        parts = ", ".join(f"{sid} {t.seconds:.1f}s/{t.words} Wörter" for sid, t in sorted(self.dropped.items()))
        return f"primary={self.primary} verworfen: {parts}"

    async def filter(self, events: AsyncIterable[Any]) -> AsyncIterator[Any]:
        """Filtert den Ereignisstrom des Default-stt_node."""
        from livekit.agents import stt

        # Neuer Stream (Pump-Neuaufbau) = neue STT-Verbindung: neu einlernen.
        self.reset("neuer STT-Stream")
        self._conn_id = None
        try:
            async for ev in events:
                if getattr(ev, "type", None) in (
                    stt.SpeechEventType.FINAL_TRANSCRIPT,
                    stt.SpeechEventType.INTERIM_TRANSCRIPT,
                ):
                    self.observe_request_id(getattr(ev, "request_id", None))
                if (
                    getattr(ev, "type", None) == stt.SpeechEventType.FINAL_TRANSCRIPT
                    and ev.alternatives
                    and ev.alternatives[0].text
                    and not self.accept(ev.alternatives[0])
                ):
                    orig = ev.alternatives[0]
                    blank = stt.SpeechData(
                        language=orig.language, text="",
                        start_time=orig.start_time, end_time=orig.end_time,
                        speaker_id=orig.speaker_id,
                    )
                    # Leeres INTERIM löscht das Zwischenergebnis in der Erkennung (sonst
                    # hängt livekit es am Turn-Ende doch an); leeres FINAL meldet
                    # "final angekommen", ohne Text in den Turn zu schreiben.
                    yield stt.SpeechEvent(
                        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        request_id=ev.request_id, alternatives=[blank],
                    )
                    yield stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=ev.request_id, alternatives=[blank],
                    )
                    continue
                yield ev
        finally:
            logger.info("[speaker] %s", self.summary())
