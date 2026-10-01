"""Gratis-Kontingent ohne Login (Oliver 01.10.2026).

Wer nicht bezahlt hat (Raum ohne Wallet), bekommt höchstens N Gesprächsminuten pro
UTC-Tag. Gesprächsminuten = Zeit, in der mindestens ein Mensch im Raum ist.

Identität ohne Login, zwei Merkmale je Raum-Ersteller:
  - anonyme ID aus dem Browser (Header X-Anon-Id, die Oberfläche setzt sie aus
    localStorage; ohne/ungültigen Header zählt nur die IP),
  - Client-IP (wie server._client_ip: letztes X-Forwarded-For-Element = das, was
    Caddy selbst angehängt hat). IPv6 zählt je /64-Netz (Review 01.10. #3: ein
    Anschluss bekommt ein ganzes /64, sonst neue Adresse = neues Kontingent).
Gezählt wird je Merkmal; das Limit greift, sobald EINES der Merkmale es erreicht.
Gespeichert werden nur SHA-256-Hashes der Merkmale, nie IP oder ID im Klartext.

Env (Minuten pro Tag, 0 = aus):
  VOICEHOOK_FREE_MIN_PER_DAY_LIVE    Default 20
  VOICEHOOK_FREE_MIN_PER_DAY_NORMAL  Default 0 (aus; offen, ob Normal mitzählt)
Annahme: Live- und Normal-Minuten zählen getrennt (je Modus ein Zähler).

Datei: $VOICEHOOK_STATE_DIR/freetier.sqlite. Der HTTP-Server prüft beim Anlegen des
Raums (402 free_limit) und merkt sich Raum -> Merkmale; der Worker liest das EINMAL
beim Start und bucht im Takt die Minuten, solange ein Mensch da ist.

free_rooms wird _KEEP_DAYS (7) Tage gehalten, länger als jede Raumzuordnung im
Server (Token-TTL max. 1 Tag). Der Live-Worker ist fail-closed: Live-Raum ohne
Wallet und ohne free_rooms-Eintrag wird abgelehnt (Review 01.10. #2). Admin-Räume
stehen mit exempt=1 drin (nicht gezählt, aber bekannt).
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path

DEFAULT_MIN_LIVE = 20.0
DEFAULT_MIN_NORMAL = 0.0
ANON_HEADER = "x-anon-id"
_ANON_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_KEEP_DAYS = 7

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS free_usage (
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    key TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, mode, key)
);
CREATE TABLE IF NOT EXISTS free_rooms (
    room TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    keys TEXT NOT NULL,
    created_at REAL NOT NULL,
    exempt INTEGER NOT NULL DEFAULT 0
);
"""


def limit_minutes(mode: str) -> float:
    """Minuten pro Tag; 0 = kein Limit. Kaputter Wert -> Default, nie unbegrenzt."""
    name, default = (
        ("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", DEFAULT_MIN_LIVE)
        if mode == "live"
        else ("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", DEFAULT_MIN_NORMAL)
    )
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return v if v >= 0 else default


def limit_seconds(mode: str) -> float:
    return limit_minutes(mode) * 60.0


def enabled(mode: str) -> bool:
    return limit_seconds(mode) > 0


def day_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))


def _hash(kind: str, value: str) -> str:
    return kind + ":" + hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def ip_bucket(ip: str) -> str:
    """IP -> Zähl-Einheit: IPv4 voll, IPv6 als /64-Netz (IPv4-gemappt -> IPv4).
    Unparsebares bleibt wie es ist (z. B. 'unknown')."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network((addr, 64), strict=False))
    return str(addr)


def identity_keys(anon_id: str | None, ip: str | None) -> list[str]:
    """Merkmale eines Anfragenden (gehasht). Ungültige Anon-ID wird ignoriert."""
    keys = []
    anon = (anon_id or "").strip()
    if _ANON_RE.match(anon):
        keys.append(_hash("anon", anon))
    ip = (ip or "").strip()
    if ip:
        keys.append(_hash("ip", ip_bucket(ip)))
    return keys


def db_path() -> Path:
    base = os.environ.get("VOICEHOOK_STATE_DIR", "/opt/voicehook/state")
    return Path(base) / "freetier.sqlite"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("PRAGMA journal_mode=WAL")
    key = str(path)
    if key not in _INITIALIZED:
        with _INIT_LOCK:
            if key not in _INITIALIZED:
                conn.executescript(_SCHEMA)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(free_rooms)").fetchall()}
                if "exempt" not in cols:
                    conn.execute("ALTER TABLE free_rooms ADD COLUMN exempt INTEGER NOT NULL DEFAULT 0")
                _INITIALIZED.add(key)
    return conn


def used_seconds(keys: Iterable[str], mode: str, now: float | None = None) -> float:
    """Höchster Tagesverbrauch über alle Merkmale (das Limit greift beim ersten)."""
    keys = list(keys)
    if not keys:
        return 0.0
    conn = connect()
    try:
        q = ",".join("?" * len(keys))
        row = conn.execute(
            f"SELECT MAX(seconds) FROM free_usage WHERE day = ? AND mode = ? AND key IN ({q})",
            (day_key(now), mode, *keys),
        ).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0.0)


def remaining_seconds(keys: Iterable[str], mode: str, now: float | None = None) -> float:
    return limit_seconds(mode) - used_seconds(keys, mode, now)


def add_seconds(keys: Iterable[str], mode: str, seconds: float, now: float | None = None) -> None:
    keys = list(keys)
    if seconds <= 0 or not keys:
        return
    day = day_key(now)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for k in keys:
                conn.execute(
                    "INSERT INTO free_usage (day, mode, key, seconds) VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(day, mode, key) DO UPDATE SET seconds = seconds + excluded.seconds",
                    (day, mode, k, float(seconds)),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def register_room(
    room: str, mode: str, keys: Iterable[str], now: float | None = None, *, exempt: bool = False
) -> None:
    """Gratis-Raum merken (Raum -> Merkmale des Erstellers), alte Einträge aufräumen.
    exempt=True: Raum ist bekannt, wird aber nicht gezählt (Admin-Testraum)."""
    now = time.time() if now is None else now
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO free_rooms (room, mode, keys, created_at, exempt) VALUES (?, ?, ?, ?, ?)",
                (room, mode, ",".join(keys), now, 1 if exempt else 0),
            )
            # Länger halten als jede Raumzuordnung (Token-TTL <= 1 Tag), sonst fiele ein
            # alter Raum aus der Zählung (Review 01.10. #2).
            conn.execute("DELETE FROM free_rooms WHERE created_at < ?", (now - _KEEP_DAYS * 86400,))
            conn.execute("DELETE FROM free_usage WHERE day < ?", (day_key(now - _KEEP_DAYS * 86400),))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def room_keys(room: str) -> tuple[str, list[str]] | None:
    """(mode, keys) eines Gratis-Raums oder None (Raum mit Wallet / anderer Weg).
    Admin-Räume (exempt) liefern leere keys: bekannt, aber nicht gezählt."""
    conn = connect()
    try:
        row = conn.execute("SELECT mode, keys, exempt FROM free_rooms WHERE room = ?", (room,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row[2]:
        return row[0], []
    return row[0], [k for k in row[1].split(",") if k]
