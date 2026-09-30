"""Monatsbudget für den Gemini-Live-Modus (Oliver 30.09.: "Sperre rein", max 10 USD/Monat).

Der Live-Worker bucht die Kosten jeder Antwort (Token-Zahlen × Preise aus live.py)
in eine kleine JSON-Datei auf der Box; der HTTP-Server liest dieselbe Datei, bevor
er einen Live-Raum anlegt. Beide Prozesse laufen auf derselben Box, deshalb reicht
eine Datei mit Dateisperre. Neuer Monat (UTC) = neuer Zähler.

Fail-closed: ist die Datei unlesbar/kaputt, gilt das Budget als aufgebraucht.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("voicehook.budget")

DEFAULT_LIMIT_USD = 10.0


def limit_usd() -> float:
    try:
        return float(os.environ.get("VOICEHOOK_LIVE_BUDGET_USD_MONTH", DEFAULT_LIMIT_USD))
    except ValueError:
        return 0.0  # kaputter Wert -> gesperrt, nie unbegrenzt


def ledger_path() -> Path:
    base = os.environ.get("VOICEHOOK_STATE_DIR", "/opt/voicehook/state")
    return Path(base) / "live-budget.json"


def month_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m", time.gmtime(time.time() if now is None else now))


def _read(fh) -> dict:  # noqa: ANN001
    fh.seek(0)
    raw = fh.read()
    return json.loads(raw) if raw.strip() else {}


def spent_usd(path: Path | None = None, now: float | None = None) -> float:
    """Diesen Monat verbrauchte USD. Fehler -> +inf (fail-closed)."""
    path = path or ledger_path()
    if not path.exists():
        return 0.0
    try:
        with open(path) as fh:
            fcntl.flock(fh, fcntl.LOCK_SH)
            data = _read(fh)
        return float(data.get(month_key(now), 0.0))
    except Exception as e:  # noqa: BLE001
        logger.error("[budget] ledger unreadable %s: %s", path, e)
        return float("inf")


def add_usd(usd: float, path: Path | None = None, now: float | None = None) -> float:
    """usd auf den laufenden Monat buchen, neuen Monatsstand zurückgeben."""
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            data = _read(fh)
        except ValueError:
            logger.error("[budget] ledger corrupt, keeping it blocked: %s", path)
            return float("inf")
        key = month_key(now)
        data[key] = round(float(data.get(key, 0.0)) + max(0.0, usd), 6)
        fh.seek(0)
        fh.truncate()
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
        return data[key]


def exhausted(path: Path | None = None, now: float | None = None) -> bool:
    return spent_usd(path, now) >= limit_usd()
