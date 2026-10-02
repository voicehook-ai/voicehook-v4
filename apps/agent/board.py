"""Status-Board des Agenten im Raum (operator.status, Oliver 02.10.2026).

Der Agent schickt bei jedem Taskwechsel das GANZE Board {doing, open[], done[]};
es ersetzt das vorherige an einem festen Platz in Deltas Instructions (nie anhängen,
damit der Kontext nicht wächst). Hartes Gesamtbudget board_budget() Zeichen (Default
BOARD_BUDGET = 2000, Env VOICEHOOK_BOARD_BUDGET, Normal und Live): zuerst fallen erledigte
Tasks weg, dann offene, zuletzt wird `doing` gekürzt.

Pull: fragt der Nutzer Delta nach dem Stand, schickt der Worker operator.status_request
an den Agenten (Code entscheidet per Muster, nicht das Modell).
"""

from __future__ import annotations

import os
import re

BOARD_BUDGET = 2000    # Zeichen über doing + open + done (Oliver 02.10.: 600 war zu knapp)
ITEM_MAX = 120         # ein Eintrag = ein Satz
LIST_MAX = 10          # Einträge je Liste vor der Budget-Kappung
_DONE_WORDS = {"fertig", "done", "erledigt", "nichts", "-"}


def _clean(v: object, cap: int = ITEM_MAX) -> str:
    s = "".join(" " if ch.isspace() else ch for ch in str(v or "") if ch.isspace() or ch.isprintable())
    s = " ".join(s.split())
    return s[:cap].rstrip()


def _items(v: object) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [c for c in (_clean(x) for x in v[:LIST_MAX]) if c]


def board_budget() -> int:
    """VOICEHOOK_BOARD_BUDGET (Default BOARD_BUDGET); ungültig oder <= 0 -> Default."""
    try:
        n = int(os.environ.get("VOICEHOOK_BOARD_BUDGET", BOARD_BUDGET))
    except (TypeError, ValueError):
        return BOARD_BUDGET
    return n if n > 0 else BOARD_BUDGET


def _size(b: dict) -> int:
    return len(b["doing"]) + sum(len(x) for x in b["open"]) + sum(len(x) for x in b["done"])


def normalize_board(payload: object) -> dict | None:
    """operator.status-Payload -> {doing, open, done} im Budget, oder None (löschen).

    Akzeptiert das Board als Dict und die Kurzform {"text": "..."} (= doing).
    Leer oder doing "fertig" ohne offene/erledigte Tasks löscht das Board.
    """
    if not isinstance(payload, dict):
        return None
    doing = _clean(payload.get("doing", payload.get("text", "")))
    b = {"doing": doing, "open": _items(payload.get("open")), "done": _items(payload.get("done"))}
    if b["doing"].lower().rstrip(".!") in _DONE_WORDS:
        b["doing"] = ""
    if not (b["doing"] or b["open"] or b["done"]):
        return None
    budget = board_budget()
    while _size(b) > budget and b["done"]:
        b["done"].pop(0)           # älteste erledigte zuerst
    while _size(b) > budget and b["open"]:
        b["open"].pop()            # hinterste offene zuerst
    if _size(b) > budget:
        b["doing"] = b["doing"][:budget].rstrip()
    return b


def board_block(board: dict | None, nom: str) -> str:
    """Fester Instructions-Abschnitt; leer ohne Board. `nom` = Name oder "dein Agent"."""
    if not board:
        return ""
    who = nom[0].upper() + nom[1:]
    parts = [f" Aktueller Stand von {who} (ersetzt jeden früheren Stand):"]
    if board["doing"]:
        parts.append(f" Gerade: {board['doing']}.")
    if board["open"]:
        parts.append(" Offen: " + "; ".join(board["open"]) + ".")
    if board["done"]:
        parts.append(" Erledigt: " + "; ".join(board["done"]) + ".")
    parts.append(
        " Fragt dein Gegenüber nach dem Stand, antwortest du kurz aus diesem Stand, mit "
        "dem Namen, zuerst was gerade läuft, offene und erledigte Tasks nur bei Bedarf."
    )
    if board["doing"]:
        parts.append(
            f" Statt eines Wartesatzes sagst du dann: Kurz Moment, {who} {board['doing']}."
        )
    return "".join(parts)


def status_sentence(board: dict | None, nom: str) -> str | None:
    """Ein gesprochener Satz zum neuen Stand (Antwort auf eine Nachfrage)."""
    if not board or not board["doing"]:
        return None
    return f"{nom[0].upper() + nom[1:]} {board['doing']}."


# Nachfrage nach dem Stand des Agenten (deutsch, umgangssprachlich).
_STATUS_Q = re.compile(
    r"\bwas\s+(?:macht|tut|treibt)\s+(?:\w+\s+){0,2}(?:gerade|grad|eigentlich|jetzt|da)\b"
    r"|\bwie\s+weit\s+(?:bist|ist|seid|sind)\b"
    r"|\b(?:wie\s+ist\s+der|was\s+ist\s+der|gib\s+mir\s+den|und\s+der)\s+(?:stand|status)\b"
    r"|\bwo\s+(?:stehst|steht)\s+(?:du|\w+)\b"
    r"|\b(?:status|zwischenstand|fortschritt)\s*\??\s*$",
    re.IGNORECASE,
)


def is_status_question(text: str) -> bool:
    return bool(_STATUS_Q.search(text or ""))
