"""Bug Oliver 01.10.2026: "die min zählen schon runter bevor ein gespräch gestartet ist
oder ein agent gesprochen hat". Stille (Mensch im Raum, kein Kostenereignis) darf den
Gratis-Topf nicht anfassen.

Absichtlich versionsneutral geprüft (alle free_usage*-Tabellen der SQLite-Datei), damit
derselbe Test auf dem alten Code (Wanduhr-Buchung) fehlschlägt: Positivkontrolle.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent import freetier
from agent.server import app

from .test_freetier import ANON_A, _run_free


def _booked_total() -> float:
    conn = sqlite3.connect(str(freetier.db_path()))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'free_usage%'")]
        total = 0.0
        for t in tables:
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            val = "ueur" if "ueur" in cols else "seconds"
            total += float(conn.execute(f"SELECT COALESCE(SUM({val}), 0) FROM {t}").fetchone()[0])
        return total
    finally:
        conn.close()


@pytest.mark.parametrize("live_mode", [False, True])
def test_silence_with_human_books_nothing(monkeypatch, live_mode):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("silent-room", "live" if live_mode else "normal", keys)
    # Mensch im Raum, kein einziges Kostenereignis (niemand spricht, Agent schweigt),
    # simulierte 5 Minuten Calldauer (time.monotonic + Uhr der Event-Loop) plus 1 s echt.
    ctx, _ = _run_free(monkeypatch, room="silent-room", humans=1, wait_s=1.0, live_mode=live_mode,
                       clock_jump_s=300.0)
    ctx.shutdown.assert_not_called()                         # Call läuft (Positivkontrolle)
    assert _booked_total() == 0                              # nichts gebucht
    me = TestClient(app).get("/api/me", headers={"x-anon-id": ANON_A, "x-forwarded-for": "1.1.1.1"})
    assert me.json()["free"]["eur_left"] == 1.0              # Topf unverändert 1,00 EUR
