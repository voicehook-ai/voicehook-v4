"""SQLite-Ledger für das Prepaid-Guthaben (portiert aus voicehook-v3 billing/db.py).

Datei: $VOICEHOOK_STATE_DIR/billing.sqlite (wie budget.py: HTTP-Server und Worker
laufen auf derselben Box und teilen sich das State-Verzeichnis). WAL + Busy-Timeout,
jede Schreiboperation in einer eigenen BEGIN-IMMEDIATE-Transaktion.

Schema:
    accounts(id, email, balance_ueur, debt_ueur, created_at, updated_at, email_verified_at)
        Konto = Wallet (geheimes Token + Wiederherstellungs-Link), NICHT die E-Mail.
        Die Checkout-E-Mail wird nur als UNBESTÄTIGTE Kontakt-Mail vermerkt; Stripe
        prüft sie nicht, deshalb führt sie allein nie zu einem Konto (Review 01.10.:
        Konto-Übernahme). email_verified_at wird erst gesetzt, wenn jemand einen
        Magic-Link an genau diese Adresse eingelöst hat (login_verified_email);
        eine bestätigte Adresse gehört höchstens einem Konto (Teilindex).
    login_links(token_hash PK, email, created_at, expires_at, used_at, requester_hash,
                nonce_hash)
        Magic-Link-Tokens (nur SHA-256), einmalig, kurzlebig (LOGIN_TTL_S).
        requester_hash = SHA-256 des Wallet-Tokens, das den Link ANGEFORDERT hat
        (Review PR #93 critical, Login-CSRF): nur genau dieses Wallet darf beim
        Einlösen mit dem Konto verknüpft werden.
        nonce_hash = SHA-256 der login_nonce, die NUR der anfordernde Browser
        bekommt (Rest-Login-CSRF, Re-Review PR #93): ohne passende Nonce wird der
        Link erst nach ausdrücklicher Bestätigung (confirm) eingelöst.
        Saldo in µEUR, brutto. debt_ueur = offener Fehlbetrag aus Erstattung/
        Rückbuchung, wird bei der nächsten Gutschrift zuerst verrechnet.
    tokens(token_hash PK, account_id, kind 'wallet'|'recovery', created_at, last_used_at)
        Nur SHA-256 der Tokens wird gespeichert; der Klartext geht genau einmal
        an den Browser (Wallet-Token -> localStorage, Recovery -> Link).
    stripe_sessions(session_id PK, account_id, amount_cents, processed_at, claimed_at,
                    payment_intent)
        Idempotenz: eine Checkout-Session wird genau einmal gutgeschrieben und
        genau einmal gegen ein Wallet-Token eingelöst.
    reversals(key PK, session_id, kind 'refund'|'dispute', cents, debited_ueur,
              shortfall_ueur, ts)
        Erstattungen/Rückbuchungen: je Schlüssel genau einmal abgezogen. Reicht der
        Saldo nicht, bleibt er bei 0 und der Fehlbetrag steht in shortfall_ueur
        (und als Schuld in accounts.debt_ueur).
    room_wallets(room PK, account_id, mode, created_at, expires_at, closed_at)
        Welches Konto zahlt für welchen Raum (erste Zuordnung gewinnt). Gilt nur
        bis expires_at (= Anlage + Token-TTL) und nur bis zum Call-Ende (closed_at,
        vom Worker gesetzt). Danach läuft kein Call mehr auf Kosten des Kontos
        (Review 01.10. #2: sonst Endlos-Calls per bekanntem Slug).
    usage(id, account_id, room, mode, usd, charge_ueur, balance_after_ueur, ts)
        Jede Abbuchung einzeln, nachvollziehbar; charge_ueur = tatsächlich abgezogen.
    oauth_states(state_hash PK, provider, code_verifier, next_path, nonce_hash,
                 requester_hash, created_at, expires_at)
        Laufende Google/GitHub-Anmeldungen (oauth_routes.py): state nur als Hash,
        einmalig (beim Callback gelöscht), OAUTH_STATE_TTL_S gültig. nonce_hash /
        requester_hash wie in login_links; der Callback legt damit einen normalen
        Login-Link an, eingelöst wird er über /api/login/verify wie der Mail-Link.
    oauth_identities(provider, subject, email, created_at, last_login_at)
        Welche Anbieter-ID (Google sub / GitHub id) zuletzt mit welcher BESTÄTIGTEN
        Adresse kam. Nur Vermerk: die Konto-Identität bleibt die E-Mail.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    balance_ueur INTEGER NOT NULL DEFAULT 0,
    debt_ueur INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS stripe_sessions (
    session_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    amount_cents INTEGER NOT NULL,
    processed_at TEXT NOT NULL,
    claimed_at TEXT,
    payment_intent TEXT
);
CREATE TABLE IF NOT EXISTS reversals (
    key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES stripe_sessions(session_id),
    kind TEXT NOT NULL,
    cents INTEGER NOT NULL,
    debited_ueur INTEGER NOT NULL,
    shortfall_ueur INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS room_wallets (
    room TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at REAL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    room TEXT NOT NULL,
    mode TEXT NOT NULL,
    usd REAL NOT NULL,
    charge_ueur INTEGER NOT NULL,
    balance_after_ueur INTEGER NOT NULL,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage(account_id);
CREATE TABLE IF NOT EXISTS login_links (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    requester_hash TEXT,
    nonce_hash TEXT
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    next_path TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    requester_hash TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_identities (
    provider TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
CREATE INDEX IF NOT EXISTS idx_sessions_pi ON stripe_sessions(payment_intent);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_verified_email
    ON accounts(email) WHERE email_verified_at IS NOT NULL;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Ältere Datei (erster PR-Stand) auf das aktuelle Schema heben. Idempotent."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
    if row and "UNIQUE" in (row[0] or "").upper():  # E-Mail darf nicht mehr eindeutig sein
        conn.executescript(
            "PRAGMA foreign_keys=OFF;"
            "BEGIN IMMEDIATE;"
            "CREATE TABLE accounts_new (id TEXT PRIMARY KEY, email TEXT NOT NULL,"
            " balance_ueur INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "INSERT INTO accounts_new SELECT id, email, balance_ueur, created_at, updated_at FROM accounts;"
            "DROP TABLE accounts;"
            "ALTER TABLE accounts_new RENAME TO accounts;"
            "COMMIT;"
            "PRAGMA foreign_keys=ON;"
        )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(stripe_sessions)").fetchall()}
    if "payment_intent" not in cols:
        conn.execute("ALTER TABLE stripe_sessions ADD COLUMN payment_intent TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "debt_ueur" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN debt_ueur INTEGER NOT NULL DEFAULT 0")
    if "email_verified_at" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN email_verified_at TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(room_wallets)").fetchall()}
    if "expires_at" not in cols:  # Altbestand ohne Ablauf: sofort abgelaufen (fail-closed)
        conn.execute("ALTER TABLE room_wallets ADD COLUMN expires_at REAL")
        conn.execute("UPDATE room_wallets SET expires_at = 0")
    if "closed_at" not in cols:
        conn.execute("ALTER TABLE room_wallets ADD COLUMN closed_at TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(login_links)").fetchall()}
    if "requester_hash" not in cols:  # Altbestand: Links ohne Bindung verknüpfen nie ein Wallet
        conn.execute("ALTER TABLE login_links ADD COLUMN requester_hash TEXT")
    if "nonce_hash" not in cols:  # Altbestand ohne Nonce: Einlösen nur mit confirm
        conn.execute("ALTER TABLE login_links ADD COLUMN nonce_hash TEXT")
    conn.executescript(_INDEXES)


def db_path() -> Path:
    base = os.environ.get("VOICEHOOK_STATE_DIR", "/opt/voicehook/state")
    return Path(base) / "billing.sqlite"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(sqlite3.DatabaseError):
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    key = str(path)
    if key not in _INITIALIZED_PATHS:
        with _INIT_LOCK:
            if key not in _INITIALIZED_PATHS:
                conn.executescript(_SCHEMA)
                _migrate(conn)
                _INITIALIZED_PATHS.add(key)
    return conn


class _Tx:
    """`with _Tx() as conn:` -> BEGIN IMMEDIATE ... COMMIT (ROLLBACK bei Fehler)."""

    def __enter__(self) -> sqlite3.Connection:
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ----- Konten ---------------------------------------------------------------
def _new_account(conn: sqlite3.Connection, email: str) -> str:
    """Immer ein NEUES Konto. Nie über die E-Mail suchen: Stripe Checkout prüft die
    Adresse nicht, wer eine fremde E-Mail eintippt, darf deren Konto nie erreichen."""
    email = normalize_email(email)
    acc = "acc_" + secrets.token_hex(12)
    now = _now()
    conn.execute(
        "INSERT INTO accounts (id, email, balance_ueur, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
        (acc, email, now, now),
    )
    return acc


def account(account_id: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(
            "SELECT id, email, balance_ueur, debt_ueur, created_at, updated_at, email_verified_at"
            " FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    finally:
        conn.close()


def balance_ueur(account_id: str) -> int:
    row = account(account_id)
    return int(row["balance_ueur"]) if row else 0


# ----- Stripe-Sessions (idempotent) ------------------------------------------
def record_stripe_session(
    session_id: str, email: str, amount_cents: int, *, account_id: str | None = None,
    payment_intent: str | None = None,
) -> bool:
    """Bezahlte Checkout-Session gutschreiben. True = neu gutgeschrieben, False = Duplikat.

    Ziel-Konto: `account_id` aus den Session-Metadaten (vom Server gesetzt, nur wenn
    der Checkout mit einem gültigen Wallet-Token gestartet wurde), sonst IMMER ein
    neues Konto. Die E-Mail führt nie zu einem bestehenden Konto. Prüfung
    auf Duplikat und Gutschrift laufen in EINER Transaktion: kein doppeltes Guthaben,
    auch nicht bei gleichzeitiger Wiederzustellung desselben Events.
    """
    if amount_cents <= 0:
        raise ValueError(f"record_stripe_session: amount_cents must be > 0, got {amount_cents}")
    with _Tx() as conn:
        if conn.execute(
            "SELECT 1 FROM stripe_sessions WHERE session_id = ?", (session_id,)
        ).fetchone():
            return False
        acc = None
        if account_id and conn.execute(
            "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
        ).fetchone():
            acc = account_id
        if acc is None:
            acc = _new_account(conn, email)
        now = _now()
        conn.execute(
            "INSERT INTO stripe_sessions (session_id, account_id, amount_cents, processed_at,"
            " payment_intent) VALUES (?, ?, ?, ?, ?)",
            (session_id, acc, amount_cents, now, payment_intent or None),
        )
        credit = amount_cents * 10_000  # 1 Cent = 10_000 µEUR
        debt = int(conn.execute("SELECT debt_ueur FROM accounts WHERE id = ?", (acc,)).fetchone()[0])
        offset = min(debt, credit)  # offener Fehlbetrag (Erstattung/Rückbuchung) zuerst
        conn.execute(
            "UPDATE accounts SET balance_ueur = balance_ueur + ?, debt_ueur = debt_ueur - ?,"
            " updated_at = ? WHERE id = ?",
            (credit - offset, offset, now, acc),
        )
    return True


def claim_session(session_id: str) -> tuple[str, str | None]:
    """Session gegen das Konto einlösen: ('ok', acc) | ('pending', None) | ('claimed', None).

    'pending' = Webhook noch nicht da (Browser fragt erneut). Jede Session nur einmal.
    """
    with _Tx() as conn:
        row = conn.execute(
            "SELECT account_id, claimed_at FROM stripe_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return "pending", None
        if row["claimed_at"]:
            return "claimed", None
        conn.execute(
            "UPDATE stripe_sessions SET claimed_at = ? WHERE session_id = ?", (_now(), session_id)
        )
        return "ok", row["account_id"]


# ----- Tokens ---------------------------------------------------------------
def issue_token(account_id: str, kind: str = "wallet") -> str:
    token = ("vhw_" if kind == "wallet" else "vhr_") + secrets.token_urlsafe(32)
    with _Tx() as conn:
        conn.execute(
            "INSERT INTO tokens (token_hash, account_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (_hash(token), account_id, kind, _now()),
        )
    return token


_LAST_USED_EVERY_S = 3600  # last_used_at höchstens stündlich schreiben (Lesen ohne Schreibsperre)


def account_for_token(token: str | None, kind: str = "wallet") -> str | None:
    """Konto zum Token. Reiner Lesezugriff ohne BEGIN IMMEDIATE (Review 01.10. #7):
    Saldo-Abfragen blockieren keine Abbuchung. last_used_at nur, wenn älter als 1 h."""
    if not token or len(token) > 200:
        return None
    h = _hash(token)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT account_id, last_used_at FROM tokens WHERE token_hash = ? AND kind = ?", (h, kind)
        ).fetchone()
        if row is None:
            return None
        last = row["last_used_at"]
        stale = True
        if last:
            with contextlib.suppress(ValueError):
                stale = (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() > _LAST_USED_EVERY_S
        if stale:  # Zeitstempel ist Komfort: nie warten (busy_timeout 0), Sperre -> auslassen
            conn.execute("PRAGMA busy_timeout = 0")
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", (_now(), h))
        return row["account_id"]
    finally:
        conn.close()


def redeem_recovery(code: str | None) -> tuple[str, str, str] | None:
    """Recovery-Code einlösen und dabei ROTIEREN (Review 01.10. #7): der alte Code
    gilt danach nicht mehr. Liefert (account_id, neues Wallet-Token, neuer Code)."""
    if not code or len(code) > 200:
        return None
    wallet = "vhw_" + secrets.token_urlsafe(32)
    new_code = "vhr_" + secrets.token_urlsafe(32)
    with _Tx() as conn:
        row = conn.execute(
            "SELECT account_id FROM tokens WHERE token_hash = ? AND kind = 'recovery'", (_hash(code),)
        ).fetchone()
        if row is None:
            return None
        acc = row["account_id"]
        now = _now()
        conn.execute("DELETE FROM tokens WHERE token_hash = ?", (_hash(code),))
        conn.executemany(
            "INSERT INTO tokens (token_hash, account_id, kind, created_at) VALUES (?, ?, ?, ?)",
            [(_hash(wallet), acc, "wallet", now), (_hash(new_code), acc, "recovery", now)],
        )
    return acc, wallet, new_code


def revoke_wallet_token(token: str | None) -> bool:
    """Abmelden: genau dieses Wallet-Token widerrufen (Zeile in tokens löschen, wie
    der Widerruf beim Login). Andere Tokens desselben Kontos (andere Geräte) und
    Recovery-Codes bleiben. Idempotent: unbekannt/leer -> False, kein Fehler."""
    if not token or len(token) > 200:
        return False
    with _Tx() as conn:
        cur = conn.execute(
            "DELETE FROM tokens WHERE token_hash = ? AND kind = 'wallet'", (_hash(token),)
        )
        return cur.rowcount > 0


# ----- Raum -> Konto ----------------------------------------------------------
DEFAULT_BIND_TTL_S = 3600


def bind_room(room: str, account_id: str, mode: str, ttl_seconds: float = DEFAULT_BIND_TTL_S,
              *, now: float | None = None) -> bool:
    """Konto zahlt für diesen Raum, längstens `ttl_seconds` (= Token-TTL) und nur bis
    zum Call-Ende (close_room). Erste Zuordnung gewinnt (True = neu gebunden)."""
    now = time.time() if now is None else now
    with _Tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO room_wallets (room, account_id, mode, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (room, account_id, mode, _now(), now + float(ttl_seconds)),
        )
        return cur.rowcount == 1


def room_binding(room: str, now: float | None = None) -> tuple[str, str, str] | None:
    """(account_id, mode, state) mit state 'active' | 'closed' | 'expired', oder None
    (Raum hat nie einem Konto gehört)."""
    now = time.time() if now is None else now
    conn = connect()
    try:
        row = conn.execute(
            "SELECT account_id, mode, expires_at, closed_at FROM room_wallets WHERE room = ?", (room,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    if row["closed_at"]:
        state = "closed"
    elif row["expires_at"] is None or float(row["expires_at"]) <= now:
        state = "expired"
    else:
        state = "active"
    return row["account_id"], row["mode"], state


def room_wallet(room: str, now: float | None = None) -> tuple[str, str] | None:
    """(account_id, mode) nur für eine AKTIVE Bindung (nicht beendet, nicht abgelaufen)."""
    b = room_binding(room, now)
    return (b[0], b[1]) if b and b[2] == "active" else None


def close_room(room: str) -> bool:
    """Call-Ende: Bindung schließen, danach zahlt das Konto für diesen Raum nichts mehr."""
    with _Tx() as conn:
        cur = conn.execute(
            "UPDATE room_wallets SET closed_at = ? WHERE room = ? AND closed_at IS NULL", (_now(), room)
        )
        return cur.rowcount == 1


# ----- Abbuchung --------------------------------------------------------------
def charge(account_id: str, ueur: int, *, room: str, mode: str, usd: float) -> int:
    """`ueur` vom Konto abziehen (Saldo nie unter 0), Buchung protokollieren, neuen Saldo liefern."""
    ueur = max(0, int(ueur))
    with _Tx() as conn:
        row = conn.execute(
            "SELECT balance_ueur FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            return 0
        old = int(row["balance_ueur"])
        new = max(0, old - ueur)
        now = _now()
        conn.execute(
            "UPDATE accounts SET balance_ueur = ?, updated_at = ? WHERE id = ?", (new, now, account_id)
        )
        conn.execute(
            "INSERT INTO usage (account_id, room, mode, usd, charge_ueur, balance_after_ueur, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, room, mode, float(usd), old - new, new, now),  # tatsächlich abgezogen
        )
        return new


# ----- Erstattung / Rückbuchung -------------------------------------------------
def reverse_payment(
    payment_intent: str, key: str, kind: str, cents: int, *, cumulative: bool = False
) -> dict:
    """Erstattung (`kind`='refund') oder Rückbuchung ('dispute') vom Konto abziehen.

    `key` macht die Buchung idempotent (Wiederzustellung = keine zweite Abbuchung).
    `cents` ist der Betrag, den DIESE Buchung zurücknimmt; mit `cumulative=True` die
    Gesamtsumme dieser Art (Stripe meldet bei charge.refunded `amount_refunded`
    kumuliert), abgezogen wird dann nur der Zuwachs. Immer gedeckelt auf das, was
    von der Session noch nicht zurückgenommen wurde. Der Saldo fällt nie unter 0;
    der nicht gedeckte Teil wird in reversals.shortfall_ueur vermerkt und als Schuld
    (accounts.debt_ueur) bei der nächsten Gutschrift abgezogen.
    Ergebnis: {'status': 'reversed'|'duplicate'|'unknown_payment'|'nothing', ...}.
    """
    with _Tx() as conn:
        if conn.execute("SELECT 1 FROM reversals WHERE key = ?", (key,)).fetchone():
            return {"status": "duplicate"}
        s = conn.execute(
            "SELECT session_id, account_id, amount_cents FROM stripe_sessions WHERE payment_intent = ?",
            (payment_intent,),
        ).fetchone()
        if s is None:
            return {"status": "unknown_payment"}
        done = conn.execute(
            "SELECT COALESCE(SUM(cents), 0) FROM reversals WHERE session_id = ?", (s["session_id"],)
        ).fetchone()[0]
        if cumulative:
            same = conn.execute(
                "SELECT COALESCE(SUM(cents), 0) FROM reversals WHERE session_id = ? AND kind = ?",
                (s["session_id"], kind),
            ).fetchone()[0]
            cents = int(cents) - int(same)
        cents = max(0, min(int(cents), int(s["amount_cents"]) - int(done)))
        want = cents * 10_000
        bal = conn.execute(
            "SELECT balance_ueur FROM accounts WHERE id = ?", (s["account_id"],)
        ).fetchone()
        have = int(bal["balance_ueur"]) if bal else 0
        debited = min(have, want)
        shortfall = want - debited
        now = _now()
        conn.execute(
            "INSERT INTO reversals (key, session_id, kind, cents, debited_ueur, shortfall_ueur, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, s["session_id"], kind, cents, debited, shortfall, now),
        )
        if debited or shortfall:
            conn.execute(
                "UPDATE accounts SET balance_ueur = balance_ueur - ?, debt_ueur = debt_ueur + ?,"
                " updated_at = ? WHERE id = ?",
                (debited, shortfall, now, s["account_id"]),
            )
        return {"status": "reversed" if cents else "nothing", "account_id": s["account_id"],
                "cents": cents, "debited_ueur": debited, "shortfall_ueur": shortfall}



# ----- Magic-Link-Login ---------------------------------------------------------
LOGIN_TTL_S = 15 * 60


def _token_hash_or_none(token: str | None) -> str | None:
    return _hash(token) if token and len(token) <= 200 else None


def create_login_link(email: str, *, requester_token: str | None = None,
                      ttl_seconds: float = LOGIN_TTL_S, now: float | None = None) -> tuple[str, str]:
    """Einmal-Token für einen Login-Link an `email` plus login_nonce für den
    anfordernden Browser; von beiden wird nur der Hash gespeichert.
    requester_token = Wallet-Token des anfordernden Browsers (X-Wallet-Token); nur
    dieses Wallet darf beim Einlösen verknüpft werden (Login-CSRF, PR #93).
    Liefert (token, nonce): token geht in die Mail, nonce in die POST-Antwort."""
    nonce = "vhn_" + secrets.token_urlsafe(32)
    token = _insert_login_link(email, requester_hash=_token_hash_or_none(requester_token),
                               nonce_hash=_hash(nonce), ttl_seconds=ttl_seconds, now=now)
    return token, nonce


def create_login_link_for_nonce(email: str, *, nonce_hash: str, requester_hash: str | None,
                                ttl_seconds: float = LOGIN_TTL_S, now: float | None = None) -> str:
    """Login-Link wie create_login_link, aber für eine schon ausgegebene login_nonce
    (OAuth: die Nonce ging bei /api/auth/<p>/start an den Browser, der Link entsteht
    erst im Callback). Eingelöst wird er unverändert über consume_login_link."""
    return _insert_login_link(email, requester_hash=requester_hash, nonce_hash=nonce_hash,
                              ttl_seconds=ttl_seconds, now=now)


def _insert_login_link(email: str, *, requester_hash: str | None, nonce_hash: str,
                       ttl_seconds: float, now: float | None) -> str:
    now = time.time() if now is None else now
    token = "vhl_" + secrets.token_urlsafe(32)
    with _Tx() as conn:
        conn.execute("DELETE FROM login_links WHERE expires_at < ?", (now - 86400,))
        conn.execute(
            "INSERT INTO login_links (token_hash, email, created_at, expires_at, requester_hash, nonce_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_hash(token), normalize_email(email), now, now + float(ttl_seconds),
             requester_hash, nonce_hash),
        )
    return token


class LoginLink(NamedTuple):
    """Ergebnis von consume_login_link.
    status 'ok'               Link verbraucht, Login durchführen.
    status 'confirm_required' Link NICHT verbraucht: Nonce fehlt/falsch und kein
                              confirm. Nur email (für die Maske) ist gesetzt."""
    status: str
    email: str
    requester_hash: str | None = None


def consume_login_link(token: str | None, *, nonce: str | None = None, confirm: bool = False,
                       now: float | None = None) -> LoginLink | None:
    """Token einlösen. None = unbekannt, abgelaufen oder schon benutzt.

    Rest-Login-CSRF (Re-Review PR #93): ein Link, den ein Angreifer an SEINE Adresse
    angefordert und dem Opfer geschickt hat, darf einen Browser nicht still in das
    Angreiferkonto schalten. Deshalb:
      - passende login_nonce (= derselbe Browser hat angefordert) -> 'ok', mit
        requester_hash (Wallet-Verknüpfung nach den Regeln in login_verified_email);
      - sonst ohne confirm -> 'confirm_required', Link bleibt gültig;
      - sonst mit confirm (Login auf anderem Gerät, Nutzer hat die Adresse
        bestätigt) -> 'ok' OHNE requester_hash: ein Wallet des einlösenden
        Browsers wird dann nie verknüpft."""
    if not token or len(token) > 200:
        return None
    now = time.time() if now is None else now
    with _Tx() as conn:
        row = conn.execute(
            "SELECT email, expires_at, used_at, requester_hash, nonce_hash FROM login_links"
            " WHERE token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if row is None or row["used_at"] is not None or float(row["expires_at"]) <= now:
            return None
        nonce_hash = _token_hash_or_none(nonce)
        same_browser = bool(nonce_hash and row["nonce_hash"]
                            and hmac.compare_digest(nonce_hash, row["nonce_hash"]))
        if not same_browser and not confirm:
            return LoginLink("confirm_required", row["email"])
        conn.execute("UPDATE login_links SET used_at = ? WHERE token_hash = ?", (now, _hash(token)))
        return LoginLink("ok", row["email"], row["requester_hash"] if same_browser else None)


def login_verified_email(
    email: str, *, wallet_token: str | None = None, requester_hash: str | None = None,
) -> tuple[str, str, str, bool]:
    """Login mit einer gerade per Magic-Link BESTÄTIGTEN Adresse.

    Liefert (account_id, neues Wallet-Token, neuer Recovery-Code, wallet_linked).
    wallet_linked = True, wenn das Wallet des einlösenden Browsers mit dem Konto
    verknüpft (bestätigt oder überführt) wurde.

    Login-CSRF (Review PR #93 critical): das mitgeschickte Wallet-Token zählt NUR,
    wenn es exakt das Token ist, das den Link angefordert hat (requester_hash aus
    login_links). Sonst könnte jemand einen Link an SEINE Adresse anfordern, ihn
    einem Opfer schicken, und dessen Browser würde beim Einlösen das Opfer-Wallet
    unter der Adresse des Angreifers bestätigen oder in dessen Konto überführen.
    Passt das Token nicht, wird das Wallet des Browsers gar nicht angefasst; der
    Browser bekommt nur ein Token für das Konto der bestätigten Adresse.

    Ziel-Konto:
      1. das Konto, dem diese Adresse schon bestätigt gehört, sonst
      2. das Konto des mitgeschickten Wallet-Tokens (derselbe Browser hat den Link
         angefordert UND Mail-Zugang bewiesen), wenn es noch keine bestätigte
         Adresse hat, sonst
      3. das älteste Konto mit dieser unbestätigten Kontakt-Mail (Stripe), sonst
      4. ein neues, leeres Konto.
    Alle übrigen unbestätigten Konten mit dieser Kontakt-Mail (und das des
    Wallet-Tokens, falls es unbestätigt ist) werden in das Ziel überführt: Saldo,
    Schuld und Stripe-Sessions wandern mit, ihre Tokens werden gelöscht.

    Sicherheit (PR #88 critical): wer bei Stripe eine fremde Adresse eintippt, hält
    ein Token für ein UNBESTÄTIGTES Konto. Wird ein Konto zum ersten Mal bestätigt,
    werden deshalb alle seine bisherigen Tokens gelöscht, außer dem, das dieser
    Browser gerade mitgeschickt hat. So erreicht nie jemand ein Konto, dessen
    Adresse er nicht selbst bestätigt hat, auch nicht durch Voranlegen.
    """
    email = normalize_email(email)
    keep_hash = _token_hash_or_none(wallet_token)
    if not (keep_hash and requester_hash and hmac.compare_digest(keep_hash, requester_hash)):
        keep_hash = None  # nicht der Anforderer: fremdes Wallet nie anfassen
    wallet = "vhw_" + secrets.token_urlsafe(32)
    code = "vhr_" + secrets.token_urlsafe(32)
    with _Tx() as conn:
        now = _now()
        verified = conn.execute(
            "SELECT id FROM accounts WHERE email = ? AND email_verified_at IS NOT NULL", (email,)
        ).fetchone()
        candidates = [r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE email = ? AND email_verified_at IS NULL"
            " ORDER BY created_at, id", (email,)
        ).fetchall()]
        requester = None
        if keep_hash:
            r = conn.execute(
                "SELECT a.id FROM tokens t JOIN accounts a ON a.id = t.account_id"
                " WHERE t.token_hash = ? AND t.kind = 'wallet' AND a.email_verified_at IS NULL",
                (keep_hash,),
            ).fetchone()
            requester = r["id"] if r else None
        if verified:
            target, newly = verified["id"], False
        elif requester:
            target, newly = requester, True
        elif candidates:
            target, newly = candidates[0], True
        else:
            target, newly = _new_account(conn, email), True
        merge = [c for c in candidates if c != target]
        if requester and requester != target and requester not in merge:
            merge.append(requester)
        for m in merge:
            bal, debt = conn.execute(
                "SELECT balance_ueur, debt_ueur FROM accounts WHERE id = ?", (m,)
            ).fetchone()
            conn.execute(
                "UPDATE accounts SET balance_ueur = balance_ueur + ?, debt_ueur = debt_ueur + ?,"
                " updated_at = ? WHERE id = ?", (int(bal), int(debt), now, target),
            )
            conn.execute(
                "UPDATE accounts SET balance_ueur = 0, debt_ueur = 0, updated_at = ? WHERE id = ?", (now, m)
            )
            conn.execute("UPDATE stripe_sessions SET account_id = ? WHERE account_id = ?", (target, m))
            conn.execute("DELETE FROM tokens WHERE account_id = ?", (m,))
        if newly:
            if target == requester:
                conn.execute("DELETE FROM tokens WHERE account_id = ? AND token_hash != ?", (target, keep_hash))
            else:
                conn.execute("DELETE FROM tokens WHERE account_id = ?", (target,))
            conn.execute(
                "UPDATE accounts SET email = ?, email_verified_at = ?, updated_at = ? WHERE id = ?",
                (email, now, now, target),
            )
        # Jeder Login widerruft alle bisherigen Recovery-Codes des Kontos (Re-Review
        # PR #93): gültig ist nur der neue, der gleich an diesen Browser geht.
        # Wallet-Tokens anderer Geräte bleiben angemeldet.
        conn.execute("DELETE FROM tokens WHERE account_id = ? AND kind = 'recovery'", (target,))
        conn.executemany(
            "INSERT INTO tokens (token_hash, account_id, kind, created_at) VALUES (?, ?, ?, ?)",
            [(_hash(wallet), target, "wallet", now), (_hash(code), target, "recovery", now)],
        )
    return target, wallet, code, requester is not None


# ----- OAuth (Google/GitHub, oauth_routes.py) ----------------------------------
OAUTH_STATE_TTL_S = 10 * 60


class OAuthState(NamedTuple):
    provider: str
    code_verifier: str
    next_path: str
    nonce_hash: str
    requester_hash: str | None


def create_oauth_state(provider: str, next_path: str, *, requester_token: str | None = None,
                       ttl_seconds: float = OAUTH_STATE_TTL_S,
                       now: float | None = None) -> tuple[str, str, str]:
    """Neuer OAuth-Ablauf: liefert (state, login_nonce, code_verifier).
    Gespeichert werden state und Nonce nur als Hash; der PKCE-Verifier muss im
    Klartext bleiben (er geht beim Code-Tausch an den Anbieter) und wird mit dem
    state beim Callback gelöscht."""
    now = time.time() if now is None else now
    state = secrets.token_urlsafe(32)
    nonce = "vhn_" + secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 Zeichen, RFC 7636: 43..128
    with _Tx() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO oauth_states (state_hash, provider, code_verifier, next_path, nonce_hash,"
            " requester_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash(state), provider, verifier, next_path, _hash(nonce),
             _token_hash_or_none(requester_token), now, now + float(ttl_seconds)),
        )
    return state, nonce, verifier


def consume_oauth_state(provider: str, state: str | None, *,
                        now: float | None = None) -> OAuthState | None:
    """state einlösen: einmalig (wird immer gelöscht), nur für denselben Anbieter,
    nur vor Ablauf. None = unbekannt, benutzt, abgelaufen oder falscher Anbieter."""
    if not state or len(state) > 200:
        return None
    now = time.time() if now is None else now
    with _Tx() as conn:
        row = conn.execute(
            "SELECT provider, code_verifier, next_path, nonce_hash, requester_hash, expires_at"
            " FROM oauth_states WHERE state_hash = ?", (_hash(state),),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (_hash(state),))
        if float(row["expires_at"]) <= now or row["provider"] != provider:
            return None
        return OAuthState(row["provider"], row["code_verifier"], row["next_path"],
                          row["nonce_hash"], row["requester_hash"])


def record_oauth_identity(provider: str, subject: str, email: str) -> None:
    """Anbieter-ID zur bestätigten Adresse vermerken (letzte gewinnt)."""
    now = _now()
    with _Tx() as conn:
        conn.execute(
            "INSERT INTO oauth_identities (provider, subject, email, created_at, last_login_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, subject) DO UPDATE SET"
            " email = excluded.email, last_login_at = excluded.last_login_at",
            (provider, str(subject), normalize_email(email), now, now),
        )
