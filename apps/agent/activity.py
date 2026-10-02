"""Aktivitäts-Feed des Agenten im Raum (operator.activity, Oliver 02.10.2026).

Ein Claude-Code-Hook (voicehook-agent >= 0.10.0, `voicehook-agent hook install`) schreibt
je Tool-Aufruf eine Zeile "HH:MM:SS Tool: Beschreibung"; die laufende CLI schickt die
letzten 15 Zeilen höchstens alle 5 s und nur bei Änderung. Hier wird daraus ein kurzer
Wissensblock "Zuletzt hat {Name} gemacht: ..." in Deltas Instructions, fester Platz,
ersetzt den vorigen (nie stapeln). Budget activity_budget() Zeichen (Default 800, Env
VOICEHOOK_ACTIVITY_BUDGET): die ältesten Zeilen fallen zuerst weg.

Eigenes Topic statt Board-Feld: das Board pflegt der Agent bewusst bei jedem Taskwechsel
(ganzes Board, "fertig" löscht), der Feed kommt mechanisch und oft vom Hook. Im Board
würde jede Hook-Zeile das kuratierte Board überschreiben oder mit ihm um dasselbe
Rate-Limit-Fenster konkurrieren, und eine Board-Nachfrage (status_request) würde von
einer Hook-Zeile statt vom Agenten beantwortet. Alte Worker ignorieren das Topic.

Die CLI schreibt nie Kommandos, Argumente, Pfade oder Ausgaben; der Worker scrubbt
trotzdem noch einmal offensichtliche Token (Verteidigung in der Tiefe).
"""

from __future__ import annotations

import os
import re

TOPIC_ACTIVITY = "operator.activity"
ACTIVITY_BUDGET = 800   # Zeichen über alle Zeilen (Auftrag Oliver 02.10.: ca. 800)
ACTIVITY_LINES = 15     # so viele Zeilen schickt die CLI höchstens
LINE_MAX = 160          # eine Zeile: Zeit + Tool + Beschreibung (CLI kappt auf 120)

_SECRET = re.compile(
    r"\b(?:sk|rk|pk)[_-](?:live|test)?_?[A-Za-z0-9_-]{8,}"
    r"|\b(?:re|whsec|vhw|ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}"
    r"|\bxox[abpr]-[A-Za-z0-9-]{8,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{20,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]+)*"
    r"|\bbearer\s+\S+"
    r"|\b(?:token|key|secret|password|passwd|pwd)\s*[=:]\s*\S+"
    r"|[A-Za-z0-9+/=_-]{32,}",
    re.IGNORECASE,
)


def scrub(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def activity_budget() -> int:
    """VOICEHOOK_ACTIVITY_BUDGET (Default ACTIVITY_BUDGET); ungültig oder <= 0 -> Default."""
    try:
        n = int(os.environ.get("VOICEHOOK_ACTIVITY_BUDGET", ACTIVITY_BUDGET))
    except (TypeError, ValueError):
        return ACTIVITY_BUDGET
    return n if n > 0 else ACTIVITY_BUDGET


def _clean(v: object) -> str:
    s = "".join(" " if ch.isspace() else ch for ch in str(v or "") if ch.isspace() or ch.isprintable())
    return scrub(" ".join(s.split()))[:LINE_MAX].rstrip()


def normalize_activity(payload: object) -> list[str] | None:
    """operator.activity-Payload {"lines": [...]} -> Zeilen (älteste zuerst) im Budget,
    oder None (leer: Block fällt weg). Die neuesten Zeilen bleiben, ältere fallen zuerst."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("lines")
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        return None
    lines = [c for c in (_clean(x) for x in raw[-ACTIVITY_LINES:]) if c]
    budget = activity_budget()
    while lines and sum(len(x) for x in lines) > budget:
        lines.pop(0)
    if not lines and raw:
        last = _clean(raw[-1])
        lines = [last[:budget].rstrip()] if last else []
    return lines or None


def activity_block(lines: list[str] | None, nom: str) -> str:
    """Fester Instructions-Abschnitt; leer ohne Zeilen. `nom` = Name oder "dein Agent"."""
    if not lines:
        return ""
    who = nom[0].upper() + nom[1:]
    return (f" Zuletzt hat {who} gemacht (Protokoll seiner letzten Arbeitsschritte, älteste "
            f"zuerst, ersetzt jedes frühere; gehört zum Status): " + "; ".join(
                x.rstrip(".") for x in lines) + "."
            f" Daraus darfst du erzählen, was {who} gerade macht, in der dritten Person, nur was "
            "dort steht, ohne zu übertreiben, nie Tool-Namen vorlesen.")
