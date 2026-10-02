"""Status-Board des Agenten im Raum (operator.status, Oliver 02.10.2026).

Der Agent schickt bei jedem Taskwechsel das GANZE Board {doing, open[], done[], faq[]};
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
DOING_MAX = 400        # doing: Zwischenstand + ETA (Oliver 02.10.: 120 war zu knapp)
ITEM_MAX = 200         # ein Listeneintrag (offen/erledigt)
LIST_MAX = 10          # Einträge je Liste vor der Budget-Kappung
FAQ_MAX = 6            # faq: Paare Frage -> kurze Antwort (Oliver 02.10.)
FAQ_ITEM_MAX = 200     # je Frage und je Antwort
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


def _faq(v: object) -> list[dict]:
    """faq: [{q, a}] (auch [[q, a]] oder "q::a"), höchstens FAQ_MAX Paare, je 200 Zeichen."""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, dict):
            q, a = x.get("q", x.get("question")), x.get("a", x.get("answer"))
        elif isinstance(x, (list, tuple)) and len(x) == 2:
            q, a = x
        elif isinstance(x, str) and "::" in x:
            q, a = x.split("::", 1)
        else:
            continue
        q, a = _clean(q, FAQ_ITEM_MAX), _clean(a, FAQ_ITEM_MAX)
        if q and a:
            out.append({"q": q, "a": a})
        if len(out) >= FAQ_MAX:
            break
    return out


def _size(b: dict) -> int:
    return (len(b["doing"]) + sum(len(x) for x in b["open"]) + sum(len(x) for x in b["done"])
            + sum(len(f["q"]) + len(f["a"]) for f in b.get("faq", [])))


def normalize_board(payload: object) -> dict | None:
    """operator.status-Payload -> {doing, open, done} im Budget, oder None (löschen).

    Akzeptiert das Board als Dict und die Kurzform {"text": "..."} (= doing).
    Leer oder doing "fertig" ohne offene/erledigte Tasks löscht das Board.
    """
    if not isinstance(payload, dict):
        return None
    doing = _clean(payload.get("doing", payload.get("text", "")), DOING_MAX)
    b = {"doing": doing, "open": _items(payload.get("open")), "done": _items(payload.get("done"))}
    faq = _faq(payload.get("faq"))
    if faq:
        b["faq"] = faq
    if b["doing"].lower().rstrip(".!") in _DONE_WORDS:
        b["doing"] = ""
    if not (b["doing"] or b["open"] or b["done"] or faq):
        return None
    budget = board_budget()
    while _size(b) > budget and b.get("faq"):
        b["faq"].pop()             # faq zuerst (Oliver 02.10.), hinterste Frage zuerst
    if "faq" in b and not b["faq"]:
        del b["faq"]
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
    # Neutraler Wissensblock, keine Sprechformel: "Kurz Moment, {Name} {doing}." las Delta
    # roh vor ("Kurz Moment, Claude Claude baut gerade ...", Oliver 02.10.).
    who = nom[0].upper() + nom[1:]
    fields = []
    if board["doing"]:
        fields.append(f"macht gerade: {board['doing'].rstrip('.')}")
    if board["open"]:
        fields.append("offen: " + ", ".join(x.rstrip(".") for x in board["open"]))
    if board["done"]:
        fields.append("erledigt: " + ", ".join(x.rstrip(".") for x in board["done"]))
    out = ""
    if fields:
        out = (f" Status von {who} (ersetzt jeden früheren Stand; Wissen, daraus formulierst du "
               f"eigene, natürliche Sätze): " + "; ".join(fields) + ".")
    if board.get("faq"):
        out += (f" Wahrscheinliche Fragen und Antworten von {who} (gehört zum Status; fragt der "
                "Nutzer so etwas, antworte direkt daraus): " + " ".join(
                    f"Frage: {f['q'].rstrip('?')}? Antwort: {f['a'].rstrip('.')}." for f in board["faq"]))
    return out + f" Sprich von {who} immer in der dritten Person, nie als ich."


def status_sentence(board: dict | None, nom: str) -> str | None:
    """Ein gesprochener Satz zum neuen Stand (Antwort auf eine Nachfrage)."""
    if not board or not board["doing"]:
        return None
    doing = board["doing"].rstrip(".")
    who = nom[0].upper() + nom[1:]
    if doing.lower().startswith(nom.lower()):    # "Claude baut ..." nicht zu "Claude Claude"
        doing = doing[len(nom):].lstrip(" ,:")
    return f"{who} {doing}."


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
