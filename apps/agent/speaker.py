"""Hauptsprecher-Filter hinter der Spracherkennung (Oliver 30.09.: Serie im Hintergrund
und Nachbar wurden transkribiert und landeten beim LLM).

Deepgram-Streaming mit `diarize=true` (Plugin-Option `enable_diarization`) liefert je
FINAL_TRANSCRIPT den Mehrheitssprecher des Segments als `SpeechData.speaker_id`
("S0", "S1", ...). Zwischenergebnisse (INTERIM) tragen laut Plugin keine
Sprecher-Info (`speaker_id=None`), gefiltert wird daher an den finalen Segmenten.

Hauptsprecher = der Sprecher mit der meisten bestätigten Sprechzeit (Summe der
finalen Segmente) im Call. Bis der führende Sprecher `min_primary_s` Sekunden
gesammelt hat, geht alles durch (Einlernphase). Danach werden finale Segmente
anderer Sprecher verworfen, bevor sie das LLM erreichen. Fehlt die Sprecher-Info,
geht das Segment durch (fail-open: der Call wird nie taub), einmal geloggt.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("voicehook.speaker")

# Einlernphase: so viel bestätigte Sprechzeit braucht der führende Sprecher, bevor
# gefiltert wird. Überschreibbar per VOICEHOOK_SPEAKER_MIN_S.
DEFAULT_MIN_PRIMARY_S = 3.0

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

    def __init__(self, *, min_primary_s: float | None = None) -> None:
        self.min_primary_s = _min_primary_s() if min_primary_s is None else min_primary_s
        self.heard: dict[str, _Tally] = {}     # bestätigte Sprechzeit je Sprecher
        self.dropped: dict[str, _Tally] = {}   # verworfen je Sprecher
        self.primary: str | None = None
        self._missing_logged = False

    def _update_primary(self) -> None:
        best = self.primary
        for sid, t in self.heard.items():
            cur = self.heard.get(best) if best is not None else None
            # nur bei echtem Vorsprung wechseln, sonst bleibt der bisherige
            if cur is None or (t.seconds, t.words) > (cur.seconds, cur.words):
                best = sid
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
        tally = self.heard.setdefault(sid, _Tally())
        tally.seconds += dur
        tally.words += words
        self._update_primary()
        if self.primary is None or self.heard[self.primary].seconds < self.min_primary_s:
            return True  # Einlernphase: noch nicht genug Daten
        if sid == self.primary:
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

        try:
            async for ev in events:
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
