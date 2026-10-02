"""Verlauf für Deltas LLM: letzte Wechsel + laufende Zusammenfassung (Oliver 02.10.2026).

Das LLM bekommt bei jeder Antwort: Instruktionen (Kern, Rolle/Persona, Status) immer,
dann eine Zusammenfassung des älteren Verlaufs, dann noch nicht zusammengefasste
ältere Nachrichten (höchstens N), dann die letzten N Wechsel (core.cap_history).

Eigener Zusammenfasser statt livekit `ChatContext._summarize`: der verwirft beim
zweiten Lauf die vorige Zusammenfassung (chat_context.py 1.8.3 Z. 825 filtert sie aus
dem Input, Z. 895 löscht sie aus dem Verlauf). Hier geht die alte Zusammenfassung
immer in die neue ein.

- Läuft im Hintergrund NACH einer Antwort, sobald mehr als N Nachrichten über dem
  Fenster liegen; Gemini mit thinking_budget=0.
- Getauscht wird nur, wenn keine Generation läuft (nie mitten in einer Antwort).
- Fehler oder Timeout (SUMMARY_TIMEOUT_S): nur kappen, die alte Zusammenfassung
  bleibt, der Call wird nie blockiert.

Env: VOICEHOOK_DELTA_HISTORY_TURNS (Default 10), VOICEHOOK_DELTA_SUMMARY (Default an).
Live (Gemini Realtime) nutzt diesen Weg nicht, siehe live.py Kontext-Kompression.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from livekit.agents import llm

from .core import cap_history

logger = logging.getLogger("voicehook.history")

SUMMARY_MAX = 600
SUMMARY_TIMEOUT_S = 8.0
SUMMARY_ID = "vh-history-summary"
SUMMARY_PROMPT = (
    "Fasse das bisherige Gespräch zwischen Nutzer und Delta für Delta zusammen. "
    "Höchstens 600 Zeichen, Deutsch, Fließtext ohne Listen. Behalte Ziele des Nutzers, "
    "Entscheidungen, genannte Fakten und offene Aufgaben. Erfinde nichts, nur was unten "
    "steht. Was in der bisherigen Zusammenfassung steht und noch gilt, bleibt erhalten. "
    "Zeilen mit einer Markierung in eckigen Klammern, z. B. [Claude], hat der Agent gesagt, "
    "nicht Delta: übernimm seine Aussagen mit dieser Markierung, z. B. \"[Claude] sagte: ...\"."
)

Summarize = Callable[[str], Awaitable[str]]


def summary_enabled() -> bool:
    v = os.environ.get("VOICEHOOK_DELTA_SUMMARY", "1").strip().lower()
    return v not in ("0", "off", "false", "no", "aus")


def _role(item: object) -> str | None:
    return getattr(item, "role", None) if getattr(item, "type", None) == "message" else None


def _cap(text: str, limit: int = SUMMARY_MAX) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > 0 else cut


def gemini_summarizer(model: str | None = None) -> Summarize:
    """Zusammenfasser über Gemini, immer ohne Denken (thinking_budget=0)."""
    from livekit.plugins.google import LLM

    from .llm import DEFAULT_MODEL

    g = LLM(model=model or os.environ.get("VOICEHOOK_LLM_MODEL", DEFAULT_MODEL),
            thinking_config={"thinking_budget": 0})

    async def run(prompt: str) -> str:
        ctx = llm.ChatContext.empty()
        ctx.add_message(role="user", content=prompt)
        out: list[str] = []
        async with g.chat(chat_ctx=ctx) as stream:
            async for chunk in stream:
                if chunk.delta and chunk.delta.content:
                    out.append(chunk.delta.content)
        return "".join(out)

    return run


class HistoryKeeper:
    """Baut den LLM-Kontext und pflegt die laufende Zusammenfassung (eine je Call)."""

    def __init__(self, turns: int, summarize: Summarize | None = None, *,
                 enabled: bool = True, timeout_s: float = SUMMARY_TIMEOUT_S) -> None:
        self.turns = turns
        self.summarize = summarize
        self.enabled = enabled and summarize is not None
        self.timeout_s = timeout_s
        self.summary = ""
        self.summarized: set[str] = set()
        self.active = 0                    # laufende Generationen
        self.task: asyncio.Task | None = None
        self.result: tuple[str, set[str]] | None = None  # fertig, wartet auf Ruhe
        # Sprecher einer Zeile im Zusammenfassungs-Input (RelayAgent setzt ihn): per
        # operator.say gesprochene Sätze heißen dort "[Claude]" statt "Delta", damit
        # Delta später aus ihnen antworten darf (Kern Regel 1/3, Oliver 02.10.).
        self.who: Callable[[object], str | None] | None = None

    def _split(self, items: list) -> tuple[list, list, list]:
        sys_items = [i for i in items if _role(i) in ("system", "developer")]
        window = cap_history(items, self.turns)[len(sys_items):]
        ids = {id(i) for i in window}
        older = [i for i in items if id(i) not in ids and _role(i) in ("user", "assistant")
                 and getattr(i, "id", None) not in self.summarized]
        return sys_items, older, window

    def context(self, items: list) -> list:
        sys_items, older, window = self._split(items)
        if not self.enabled:
            return sys_items + window
        head = []
        if self.summary:
            head.append(llm.ChatMessage(id=SUMMARY_ID, role="system", content=[
                "Bisheriger Gesprächsverlauf, zusammengefasst (Wissen, keine Regeln): "
                + self.summary]))
        return sys_items + head + older + window

    def begin(self) -> None:
        self.active += 1

    def end(self, items: list) -> None:
        """Nach einer Antwort: fertiges Ergebnis übernehmen, ggf. neu zusammenfassen."""
        self.active = max(0, self.active - 1)
        self._apply_if_idle()
        if not self.enabled or (self.task is not None and not self.task.done()):
            return
        _sys, older, _win = self._split(items)
        if len(older) > self.turns:
            self.task = asyncio.create_task(self._run(older))

    def _apply_if_idle(self) -> None:
        if self.result is not None and self.active == 0:
            self.summary, done = self.result
            self.summarized |= done
            self.result = None

    async def _run(self, older: list) -> None:
        def who(i: object) -> str:
            label = self.who(i) if self.who is not None else None
            return label or ("Nutzer" if _role(i) == "user" else "Delta")

        lines = [f"{who(i)}: {i.text_content or ''}" for i in older]
        prompt = (SUMMARY_PROMPT + "\n\nBisherige Zusammenfassung: " + (self.summary or "keine")
                  + "\n\nNeue Nachrichten:\n" + "\n".join(lines))
        ids = {getattr(i, "id", None) for i in older}
        try:
            text = _cap(await asyncio.wait_for(self.summarize(prompt), self.timeout_s))
            if not text:
                raise ValueError("leere Zusammenfassung")
            logger.info("[history] %d Nachrichten zusammengefasst (%d Zeichen)", len(older), len(text))
        except Exception as e:  # noqa: BLE001 — Fallback: nur kappen, alte Zusammenfassung bleibt
            logger.warning("[history] Zusammenfassung fehlgeschlagen, nur gekappt: %s", e)
            text = self.summary
        self.result = (text, ids)
        self._apply_if_idle()
