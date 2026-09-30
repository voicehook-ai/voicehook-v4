"""Sprachfilter vor der Spracherkennung (Oliver 30.09.: Kosten laufen in der Stille hoch).

Deepgram-Streaming rechnet jede Sekunde Audio ab, die wir schicken, auch Stille.
SpeechGate lässt nur Audio durch, in dem Silero-VAD Sprache erkennt, plus einen
Vorlauf (damit der Wortanfang nicht fehlt) und einen Nachlauf (damit Deepgram das
Satzende erkennt). In der Stille hält der Deepgram-Stream seine Verbindung per
KeepAlive offen und bekommt kein Audio, also keine Kosten.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

logger = logging.getLogger("voicehook.gate")

DEFAULT_PREROLL_S = 0.5
DEFAULT_HANGOVER_S = 1.0


def gate_enabled() -> bool:
    return os.environ.get("VOICEHOOK_STT_GATE", "1").strip() not in ("0", "false", "off", "")


def _frame_seconds(frame: Any) -> float:
    rate = getattr(frame, "sample_rate", 0) or 0
    return (getattr(frame, "samples_per_channel", 0) or 0) / rate if rate else 0.0


class SpeechGate:
    """Filtert einen Audio-Stream auf Sprachabschnitte (VAD-gesteuert)."""

    def __init__(
        self,
        vad: Any,
        *,
        preroll_s: float = DEFAULT_PREROLL_S,
        hangover_s: float = DEFAULT_HANGOVER_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._vad = vad
        self._preroll_s = preroll_s
        self._hangover_s = hangover_s
        self._clock = clock
        self.passed_s = 0.0   # an die Spracherkennung weitergegeben
        self.dropped_s = 0.0  # zurückgehalten (Stille)

    async def filter(self, audio: AsyncIterable[Any]) -> AsyncIterator[Any]:
        from livekit.agents.vad import VADEventType

        stream = self._vad.stream()
        state = {"speaking": False, "open_until": 0.0}

        async def _watch() -> None:
            async for ev in stream:
                if ev.type == VADEventType.START_OF_SPEECH:
                    state["speaking"] = True
                elif ev.type == VADEventType.END_OF_SPEECH:
                    state["speaking"] = False
                    state["open_until"] = self._clock() + self._hangover_s

        watcher = asyncio.create_task(_watch())
        buf: deque = deque()
        buf_s = 0.0
        try:
            async for frame in audio:
                stream.push_frame(frame)
                await asyncio.sleep(0)  # VAD-Ereignisse zum Zug kommen lassen
                dur = _frame_seconds(frame)
                if state["speaking"] or self._clock() < state["open_until"]:
                    while buf:
                        old = buf.popleft()
                        self.passed_s += _frame_seconds(old)
                        yield old
                    buf_s = 0.0
                    self.passed_s += dur
                    yield frame
                else:
                    buf.append(frame)
                    buf_s += dur
                    while buf and buf_s > self._preroll_s + 1e-9:
                        old = buf.popleft()
                        d = _frame_seconds(old)
                        buf_s -= d
                        self.dropped_s += d
        finally:
            watcher.cancel()
            try:
                stream.end_input()
                await stream.aclose()
            except Exception as e:  # noqa: BLE001
                logger.debug("[gate] vad close: %s", e)
            logger.info("[gate] passed=%.1fs dropped=%.1fs", self.passed_s, self.dropped_s)


def load_vad() -> Any:
    from livekit.plugins import silero

    return silero.VAD.load()
