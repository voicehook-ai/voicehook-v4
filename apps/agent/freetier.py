"""Gratis-Kontingent ohne Login (Oliver 01.10.2026, Euro-Topf seit 01.10. abends).

Wer nicht bezahlt hat, bekommt pro UTC-Tag einen Gratis-Verbrauch von 0,30 EUR
(Kundenpreis inkl. Faktor und MwSt, billing/pricing.charge_ueur). Normal und Live
teilen sich den Topf. Gebucht wird NUR aus echten Kostenereignissen des Workers
(metrics_collected), nie aus Zeit: Stille ohne Kosten zählt nichts herunter
(Bug Oliver 01.10.: "die min zählen schon runter bevor ein gespräch gestartet ist").

Identität ohne Login, zwei Merkmale je Raum-Ersteller:
  - anonyme ID aus dem Browser (Header X-Anon-Id, die Oberfläche setzt sie aus
    localStorage; ohne/ungültigen Header zählt nur die IP),
  - Client-IP (wie server._client_ip: letztes X-Forwarded-For-Element = das, was
    Caddy selbst angehängt hat). IPv6 zählt je /64-Netz (Review 01.10. #3: ein
    Anschluss bekommt ein ganzes /64, sonst neue Adresse = neues Kontingent).
Gezählt wird je Merkmal; der Rest ist der des knappsten Merkmals (MAX über die Keys).
Gespeichert werden nur SHA-256-Hashes der Merkmale, nie IP oder ID im Klartext.

Env VH_FREE_EUR_PER_DAY (Euro pro UTC-Tag), Default 0.30 (Oliver 02.10.):
  0           Gratis aus (wie früher Limit 0: Räume laufen ohne Zählung, es sei denn
              VOICEHOOK_REQUIRE_CREDITS_<MODE>=1),
  kaputt      (kein Zahlwert, negativ, inf/nan) -> 0 EUR Gratis, die Prüfung bleibt
              aber an: ohne Wallet 402 free_limit, nie unbegrenzt.
Die alten Minuten-Envs (VOICEHOOK_FREE_MIN_PER_DAY_*) werden ignoriert, die alte
Tabelle free_usage (Sekunden) bleibt liegen und wird nicht mehr gelesen.

Einheit intern Mikro-Euro (µEUR, int) wie das Wallet. Reihenfolge im Call: erst der
Gratis-Topf, dann das Guthaben eines Wallets; das Kostenereignis, das die Grenze
überschreitet, füllt den Topf bis 0 und gibt den Überhang ans Wallet (consume_ueur).

Datei: $VOICEHOOK_STATE_DIR/freetier.sqlite. Der HTTP-Server prüft beim Anlegen des
Raums (402 free_limit) und merkt sich Raum -> Merkmale; der Worker liest das EINMAL
beim Start und bucht danach je Kostenereignis.

free_rooms wird _KEEP_DAYS (7) Tage gehalten, länger als jede Raumzuordnung im
Server (Token-TTL max. 1 Tag). Der Worker ist in BEIDEN Modi fail-closed, solange
das Gratis-Kontingent an ist: Raum ohne Wallet und ohne free_rooms-Eintrag wird
abgelehnt (Review 01.10. #2, Normal seit PR #93). Admin-/Operator-Räume stehen mit
exempt=1 drin (nicht gezählt, aber bekannt): admin/live-room, und Normal-Räume, die
jemand mit einer gültigen HMAC-Einladung betritt, die der Server nicht selbst
ausgestellt hat (call-starten, register_room_if_absent).

Env VH_FREE_EXEMPT_KEYS (Oliver 01.10.: "nimm das limit für mich raus"): kommagetrennte
Liste gehashter Merkmale im Format von identity_keys() ("anon:<sha256>", "ip:<sha256>"),
keine Klartext-IPs. Trifft EINES der Merkmale eines Anfragenden bzw. Raums, ist Gratis
unbegrenzt: kein 402 free_limit, kein Call-Ende wegen free_limit, nichts wird gebucht,
/api/me zeigt eur_left = eur_per_day plus exempt=true. Wallet und die Live-Monatssperre
(budget.py) bleiben unverändert. Leer/fehlend: keine Ausnahme; kaputte Einträge werden
ignoriert. Keys erzeugen (lokal auf der Box): python -m agent.freetier keys --ip <ip> --anon <id>
Bleibt aus Abwärtskompatibilität; der Owner hängt seit 02.10. am Konto (nächster Absatz).

Env VH_FREE_EXEMPT_ACCOUNTS (Oliver 02.10.: Ausnahme am angemeldeten Konto statt an
IP-/Browser-Hashes): kommagetrennte Liste "github:<id>" / "google:<sub>", die STABILEN
Anbieter-IDs aus billing.sqlite oauth_identities.subject (GitHub: numerische User-ID,
nicht der umbenennbare Login; Google: OpenID sub, nicht die Mail). Gilt nur für einen
ANGEMELDETEN Anfragenden: X-Wallet-Token -> Konto mit bestätigter Mail
(accounts.email_verified_at), und diese Mail ist die, mit der der gelistete Anbieter-
Zugang zuletzt kam (oauth_identities.email). Ohne Login nie (IP/anon-id spielen keine
Rolle). Der Server merkt sich beim Raumanlegen das Konto in free_rooms.account; der
Worker prüft damit über dieselbe Funktion (account_exempt in free_state/consume_ueur).
Lesefehler -> keine Ausnahme (normal gezählt).

Globaler Gratis-Deckel (Oliver 02.10., INTERN, nirgends nach außen erwähnen): fester
Monatstopf in ECHTEN Kosten (cost_usd x USD_EUR, Olivers Geld, nicht Kundenpreis),
Env VH_FREE_POT_EUR_MONTH (Default 60; kaputt/negativ -> 0 = gesperrt, nie unbegrenzt).
Tagesbudget dynamisch (UTC):
    budget_heute = (Monatstopf - verbraucht im Monat VOR heute) / verbleibende Tage inkl. heute
Nicht Genutztes verteilt sich so automatisch auf die Resttage (Start 60/30 = 2 EUR/Tag).
Heute verbraucht >= budget_heute -> Gratis für ALLE leer bis zum nächsten UTC-Tag: neue
Räume 402 free_limit (dieselbe Meldung, kein Hinweis auf den Topf), /api/me eur_left 0,
laufende Calls wie beim persönlichen Limit (Wallet zahlt weiter, sonst Ansage + Ende).
Gebucht wird im selben Kostenereignis und in derselben Transaktion wie der persönliche
Topf (consume_ueur) in die Tabelle free_pot. Lesefehler -> leer (fail-closed).
Owner (VH_FREE_EXEMPT_KEYS) und Admin-Räume buchen nicht und werden nicht gesperrt.
Die Live-Monatssperre (budget.py) gilt zusätzlich, es greift die strengere.
"""

from __future__ import annotations

import calendar
import contextlib
import hashlib
import ipaddress
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path

DEFAULT_EUR_PER_DAY = 0.30
ENV_EUR_PER_DAY = "VH_FREE_EUR_PER_DAY"
DEFAULT_POT_EUR_MONTH = 60.0
ENV_POT_EUR_MONTH = "VH_FREE_POT_EUR_MONTH"
ENV_EXEMPT_KEYS = "VH_FREE_EXEMPT_KEYS"
ENV_EXEMPT_ACCOUNTS = "VH_FREE_EXEMPT_ACCOUNTS"
_ACCOUNT_RE = re.compile(r"^(github|google):([A-Za-z0-9_.@+-]{1,128})$")
_KEY_RE = re.compile(r"^(anon|ip):[0-9a-f]{64}$")
UEUR_PER_EUR = 1_000_000
ANON_HEADER = "x-anon-id"
_ANON_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_KEEP_DAYS = 7

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

# free_usage (Sekunden) ist Altlast des Minuten-Modells: bleibt, wird nur noch aufgeräumt.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS free_usage (
    day TEXT NOT NULL,
    mode TEXT NOT NULL,
    key TEXT NOT NULL,
    seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, mode, key)
);
CREATE TABLE IF NOT EXISTS free_usage_eur (
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    ueur INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, key)
);
CREATE TABLE IF NOT EXISTS free_pot (
    month TEXT NOT NULL,
    day TEXT NOT NULL,
    ueur INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day)
);
CREATE TABLE IF NOT EXISTS free_rooms (
    room TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    keys TEXT NOT NULL,
    created_at REAL NOT NULL,
    exempt INTEGER NOT NULL DEFAULT 0,
    account TEXT
);
"""


def _parse_eur() -> tuple[float, bool]:
    """(Euro pro Tag, Prüfung an). Leer -> Default; 0 -> aus; kaputt -> 0 EUR, Prüfung an."""
    raw = os.environ.get(ENV_EUR_PER_DAY, "").strip()
    if not raw:
        return DEFAULT_EUR_PER_DAY, True
    try:
        v = float(raw)
    except ValueError:
        return 0.0, True
    if not math.isfinite(v) or v < 0:
        return 0.0, True
    return v, v > 0


def limit_eur() -> float:
    """Gratis-Euro pro UTC-Tag (0 = kein Gratis)."""
    return _parse_eur()[0]


def limit_ueur() -> int:
    return round(limit_eur() * UEUR_PER_EUR)


def enabled(mode: str | None = None) -> bool:
    """Gratis-Prüfung an (beide Modi gemeinsam; `mode` nur für alte Aufrufer)."""
    return _parse_eur()[1]


def day_key(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))


# ----- Globaler Gratis-Deckel (intern, siehe Modul-Doku) ----------------------


def pot_eur_month() -> float:
    """Monatstopf in echten EUR. Leer -> Default; kaputt/negativ/inf -> 0 (gesperrt)."""
    raw = os.environ.get(ENV_POT_EUR_MONTH, "").strip()
    if not raw:
        return DEFAULT_POT_EUR_MONTH
    try:
        v = float(raw)
    except ValueError:
        return 0.0
    return v if math.isfinite(v) and v >= 0 else 0.0


def _month_days_left(now: float) -> tuple[str, int]:
    """(Monat 'YYYY-MM', verbleibende UTC-Tage inkl. heute)."""
    t = time.gmtime(now)
    days = calendar.monthrange(t.tm_year, t.tm_mon)[1]
    return time.strftime("%Y-%m", t), days - t.tm_mday + 1


def pot_budget_today_ueur(month_budget_ueur: int, used_before_today_ueur: int, days_left: int) -> int:
    """Tagesbudget = (Monatstopf - Verbrauch vor heute) / Resttage inkl. heute, abgerundet."""
    rest = max(0, int(month_budget_ueur) - int(used_before_today_ueur))
    return rest // max(1, int(days_left))


def _pot_read(conn: sqlite3.Connection, now: float) -> dict:
    month, days_left = _month_days_left(now)
    day = day_key(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN day < ? THEN ueur ELSE 0 END), 0),"
        " COALESCE(SUM(CASE WHEN day = ? THEN ueur ELSE 0 END), 0)"
        " FROM free_pot WHERE month = ?",
        (day, day, month),
    ).fetchone()
    before, today = int(row[0]), int(row[1])
    month_ueur = round(pot_eur_month() * UEUR_PER_EUR)
    budget = pot_budget_today_ueur(month_ueur, before, days_left)
    return {
        "month": month, "day": day, "days_left": days_left,
        "month_budget_ueur": month_ueur, "month_used_ueur": before + today,
        "budget_today_ueur": budget, "today_used_ueur": today,
        "left_today_ueur": max(0, budget - today),
    }


def pot_status(now: float | None = None) -> dict:
    """Stand des Monatstopfs (echte Kosten, µEUR). Wirft bei Lesefehlern."""
    now = time.time() if now is None else now
    conn = connect()
    try:
        return _pot_read(conn, now)
    finally:
        conn.close()


def pot_left_ueur(now: float | None = None) -> int:
    """Heute noch im Topf (echte µEUR). Lesefehler -> 0 (fail-closed)."""
    try:
        return pot_status(now)["left_today_ueur"]
    except Exception:  # noqa: BLE001
        return 0


def add_pot_ueur(real_ueur: int, now: float | None = None) -> None:
    """Echte Kosten direkt in den Topf buchen (Tests/Altlast; der Call bucht über consume_ueur)."""
    if real_ueur <= 0:
        return
    now = time.time() if now is None else now
    conn = connect()
    try:
        _pot_add(conn, int(real_ueur), now)
        conn.commit()
    finally:
        conn.close()


def _pot_add(conn: sqlite3.Connection, real_ueur: int, now: float) -> None:
    conn.execute(
        "INSERT INTO free_pot (month, day, ueur) VALUES (?, ?, ?)"
        " ON CONFLICT(day) DO UPDATE SET ueur = ueur + excluded.ueur",
        (time.strftime("%Y-%m", time.gmtime(now)), day_key(now), int(real_ueur)),
    )


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


def exempt_keys() -> frozenset[str]:
    """Gehashte Merkmale ohne Gratis-Limit (VH_FREE_EXEMPT_KEYS). Kaputte Einträge fallen weg."""
    raw = os.environ.get(ENV_EXEMPT_KEYS, "")
    return frozenset(k for k in (p.strip().lower() for p in raw.split(",")) if _KEY_RE.match(k))


def exempt_accounts() -> frozenset[tuple[str, str]]:
    """(Anbieter, Subject) ohne Gratis-Limit (VH_FREE_EXEMPT_ACCOUNTS). Kaputte Einträge fallen weg."""
    out = set()
    for part in os.environ.get(ENV_EXEMPT_ACCOUNTS, "").split(","):
        m = _ACCOUNT_RE.match(part.strip())
        if m:
            out.add((m.group(1), m.group(2)))
    return frozenset(out)


def account_exempt(account_id: str | None) -> bool:
    """Angemeldetes Konto in VH_FREE_EXEMPT_ACCOUNTS? Nur Konten mit bestätigter Mail
    (= angemeldet); deren Mail muss die sein, mit der ein gelisteter Anbieter-Zugang
    kam (oauth_identities). Kein Konto/leere Env -> False ohne DB-Zugriff.
    Lesefehler -> False (dann gilt das normale Limit)."""
    if not account_id:
        return False
    allowed = exempt_accounts()
    if not allowed:
        return False
    from .billing import db as billing_db  # spät: freetier bleibt ohne Billing importierbar

    try:
        conn = billing_db.connect()
        try:
            rows = conn.execute(
                "SELECT o.provider, o.subject FROM accounts a"
                " JOIN oauth_identities o ON o.email = a.email"
                " WHERE a.id = ? AND a.email_verified_at IS NOT NULL",
                (account_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False
    return any((r[0], str(r[1])) in allowed for r in rows)


def is_exempt(keys: Iterable[str] | None, account: str | None = None) -> bool:
    """Ausnahme? Merkmal in VH_FREE_EXEMPT_KEYS ODER angemeldetes Konto in
    VH_FREE_EXEMPT_ACCOUNTS. Leere Listen/Env -> False."""
    allowed = exempt_keys()
    if allowed and any(k in allowed for k in (keys or ())):
        return True
    return account_exempt(account)


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
                if "account" not in cols:
                    conn.execute("ALTER TABLE free_rooms ADD COLUMN account TEXT")
                _INITIALIZED.add(key)
    return conn


def used_ueur(keys: Iterable[str], now: float | None = None) -> int:
    """Höchster Tagesverbrauch über alle Merkmale (der Topf ist leer beim ersten)."""
    keys = list(keys)
    if not keys:
        return 0
    conn = connect()
    try:
        q = ",".join("?" * len(keys))
        row = conn.execute(
            f"SELECT MAX(ueur) FROM free_usage_eur WHERE day = ? AND key IN ({q})",
            (day_key(now), *keys),
        ).fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def free_state(
    keys: Iterable[str] | None, now: float | None = None, *, account: str | None = None
) -> dict:
    """DIE eine Gratis-Rechnung (Oliver 02.10.: "Frontend und Agenten brauchen die GLEICHEN
    Werte"). /api/me, /api/free/remaining, die 402-Entscheidung beim Raumanlegen und der
    Worker (Start-Prüfung, nach jeder Buchung, Topic free.state) lesen NUR hier.

    Liefert {left_ueur, eur_left, eur_per_day, exempt, pot_empty, reason}:
      reason None              Rest > 0, Gratis läuft
             "disabled"        Gratis aus (VH_FREE_EUR_PER_DAY=0): eur_left 0
             "exempt"          Merkmal in VH_FREE_EXEMPT_KEYS oder angemeldetes Konto in
                               VH_FREE_EXEMPT_ACCOUNTS (`account`): voller Tageswert, nie leer
             "personal_limit"  eigener Tageswert verbraucht
             "pot_empty"       globaler Topf heute leer (oder unlesbar, fail-closed)
    Lesefehler der persönlichen Zählung werfen (Aufrufer entscheiden fail-closed)."""
    keys = list(keys or ())
    if not enabled():
        return {"left_ueur": 0, "eur_left": 0.0, "eur_per_day": 0.0, "exempt": False,
                "pot_empty": False, "reason": "disabled"}
    limit = limit_ueur()
    per_day = round(limit_eur(), 2)
    if is_exempt(keys, account):
        return {"left_ueur": limit, "eur_left": _floor_eur(limit), "eur_per_day": per_day,
                "exempt": True, "pot_empty": False, "reason": "exempt"}
    personal = max(0, limit - used_ueur(keys, now))
    pot_empty = pot_left_ueur(now) <= 0
    left = 0 if pot_empty else personal
    reason = None if left > 0 else ("pot_empty" if pot_empty and personal > 0 else "personal_limit")
    return {"left_ueur": left, "eur_left": _floor_eur(left), "eur_per_day": per_day,
            "exempt": False, "pot_empty": pot_empty, "reason": reason}


def _floor_eur(ueur: int) -> float:
    """µEUR -> Euro, 2 Nachkommastellen, abgerundet (nie mehr anzeigen als da ist)."""
    return math.floor(int(ueur) / 10_000) / 100


def remaining_ueur(keys: Iterable[str], now: float | None = None) -> int:
    """Gratis-Rest heute (dünne Hülle um free_state, keine eigene Rechnung)."""
    return free_state(keys, now)["left_ueur"]


def remaining_eur(keys: Iterable[str], now: float | None = None) -> float:
    """Rest in Euro (dünne Hülle um free_state)."""
    return free_state(keys, now)["eur_left"]


def _add(conn: sqlite3.Connection, keys: list[str], ueur: int, day: str) -> None:
    for k in keys:
        conn.execute(
            "INSERT INTO free_usage_eur (day, key, ueur) VALUES (?, ?, ?)"
            " ON CONFLICT(day, key) DO UPDATE SET ueur = ueur + excluded.ueur",
            (day, k, int(ueur)),
        )


def add_ueur(keys: Iterable[str], ueur: int, now: float | None = None) -> None:
    """Verbrauch auf jedes Merkmal buchen (ohne Deckel; Tests und Altlast)."""
    keys = list(keys)
    if ueur <= 0 or not keys:
        return
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _add(conn, keys, ueur, day_key(now))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def consume_ueur(
    keys: Iterable[str], ueur: int, now: float | None = None, *, real_ueur: int | None = None,
    account: str | None = None,
) -> tuple[int, int]:
    """Kostenereignis aus dem Gratis-Topf nehmen, atomar (parallele Räume derselben
    Identität teilen den Topf). Liefert (aus dem Topf genommen, Rest danach).
    Überhang = ueur - genommen geht ans Wallet.

    `ueur` ist der Kundenpreis (persönlicher Topf), `real_ueur` die echten Kosten
    desselben Ereignisses (globaler Monatstopf). Ohne `real_ueur` wird konservativ
    der Kundenpreis in den Monatstopf gebucht (nie zu wenig). Reicht der globale Topf
    nur teilweise, wird anteilig genommen; danach ist der Rest 0 (für alle leer).
    Ausnahme (VH_FREE_EXEMPT_KEYS / VH_FREE_EXEMPT_ACCOUNTS): alles gilt als gedeckt,
    nichts wird gebucht."""
    keys = list(keys)
    if not keys:
        return 0, 0
    if is_exempt(keys, account):
        return max(0, int(ueur)), limit_ueur()
    now = time.time() if now is None else now
    ueur = int(ueur)
    real = int(ueur if real_ueur is None else real_ueur)
    day = day_key(now)
    limit = limit_ueur()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            q = ",".join("?" * len(keys))
            row = conn.execute(
                f"SELECT MAX(ueur) FROM free_usage_eur WHERE day = ? AND key IN ({q})", (day, *keys)
            ).fetchone()
            left = max(0, limit - int(row[0] or 0))
            pot_left = _pot_read(conn, now)["left_today_ueur"]
            take = max(0, min(ueur, left))
            if pot_left <= 0:
                take = 0
            elif real > pot_left and take > 0:
                take = min(take, ueur * pot_left // real)  # anteilig, bis der Topf genau leer ist
            if take > 0:
                _add(conn, keys, take, day)
                real_take = real if take >= ueur else -(-real * take // max(1, ueur))
                _pot_add(conn, min(real_take, pot_left), now)
                pot_left -= min(real_take, pot_left)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    rest = left - take
    if pot_left <= 0:
        rest = 0
    return take, rest


def register_room(
    room: str, mode: str, keys: Iterable[str], now: float | None = None, *, exempt: bool = False,
    account: str | None = None,
) -> None:
    """Gratis-Raum merken (Raum -> Merkmale des Erstellers), alte Einträge aufräumen.
    exempt=True: Raum ist bekannt, wird aber nicht gezählt (Admin-Testraum).
    account: Konto des angemeldeten Erstellers (für VH_FREE_EXEMPT_ACCOUNTS im Worker)."""
    now = time.time() if now is None else now
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO free_rooms (room, mode, keys, created_at, exempt, account)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (room, mode, ",".join(keys), now, 1 if exempt else 0, account or None),
            )
            # Länger halten als jede Raumzuordnung (Token-TTL <= 1 Tag), sonst fiele ein
            # alter Raum aus der Zählung (Review 01.10. #2).
            conn.execute("DELETE FROM free_rooms WHERE created_at < ?", (now - _KEEP_DAYS * 86400,))
            old = day_key(now - _KEEP_DAYS * 86400)
            conn.execute("DELETE FROM free_usage_eur WHERE day < ?", (old,))
            conn.execute("DELETE FROM free_usage WHERE day < ?", (old,))  # Altlast (Sekunden)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def register_room_if_absent(room: str, mode: str, now: float | None = None) -> bool:
    """Raum als Operator-Raum (exempt, ungezählt) merken, wenn er noch keinen Eintrag
    hat; bestehende Einträge bleiben unverändert. True = neu angelegt.

    Für Räume, die nur mit gültiger HMAC-Einladung betreten werden, die der Server
    selbst nie ausgestellt hat (call-starten: Operator mintet mit INVITE_SECRET).
    Wer das Secret hat, ist Admin; ohne Eintrag würde der Worker den Raum seit
    PR #93 abweisen (fail-closed auch im Normalmodus)."""
    now = time.time() if now is None else now
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO free_rooms (room, mode, keys, created_at, exempt) VALUES (?, ?, '', ?, 1)",
            (room, mode, now),
        )
        conn.commit()
        return cur.rowcount > 0
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


def room_account(room: str) -> str | None:
    """Konto des angemeldeten Raum-Erstellers (free_rooms.account) oder None."""
    conn = connect()
    try:
        row = conn.execute("SELECT account FROM free_rooms WHERE room = ?", (room,)).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def _main(argv: list[str] | None = None) -> int:
    """CLI: gehashte Merkmale für VH_FREE_EXEMPT_KEYS ausgeben (lokal auf der Box)."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m agent.freetier")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keys", help="gehashte Merkmale für VH_FREE_EXEMPT_KEYS")
    k.add_argument("--ip", help="Client-IP (IPv6 zählt je /64)")
    k.add_argument("--anon", help="X-Anon-Id des Browsers (localStorage)")
    a = ap.parse_args(argv)
    if a.anon and not _ANON_RE.match(a.anon.strip()):
        ap.error("--anon ungültig (8-128 Zeichen A-Za-z0-9_-)")
    keys = identity_keys(a.anon, a.ip)
    if not keys:
        ap.error("mindestens --ip oder --anon angeben")
    for key in keys:
        print(key)
    print(f"{ENV_EXEMPT_KEYS}={','.join(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
