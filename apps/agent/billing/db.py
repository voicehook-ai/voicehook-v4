"""SQLite-Ledger für das Prepaid-Guthaben (portiert aus voicehook-v3 billing/db.py).

Datei: $VOICEHOOK_STATE_DIR/billing.sqlite (wie budget.py: HTTP-Server und Worker
laufen auf derselben Box und teilen sich das State-Verzeichnis). WAL + Busy-Timeout,
jede Schreiboperation in einer eigenen BEGIN-IMMEDIATE-Transaktion.

Schema:
    accounts(id, email, balance_ueur, created_at, updated_at)
        Konto = Wallet (geheimes Token + Wiederherstellungs-Link), NICHT die E-Mail.
        Die Checkout-E-Mail wird nur vermerkt; Stripe prüft sie nicht, deshalb wird
        NIE über die E-Mail zusammengeführt (Review 01.10.: Konto-Übernahme).
        Saldo in µEUR, brutto.
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
        Saldo nicht, bleibt er bei 0 und der Fehlbetrag steht in shortfall_ueur.
    room_wallets(room PK, account_id, mode, created_at)
        Welches Konto zahlt für welchen Raum (erste Zuordnung gewinnt).
    usage(id, account_id, room, mode, usd, charge_ueur, balance_after_ueur, ts)
        Jede Abbuchung einzeln, nachvollziehbar.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    balance_ueur INTEGER NOT NULL DEFAULT 0,
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
    created_at TEXT NOT NULL
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
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email);
CREATE INDEX IF NOT EXISTS idx_sessions_pi ON stripe_sessions(payment_intent);
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
            "SELECT id, email, balance_ueur, created_at, updated_at FROM accounts WHERE id = ?",
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
        conn.execute(
            "UPDATE accounts SET balance_ueur = balance_ueur + ?, updated_at = ? WHERE id = ?",
            (amount_cents * 10_000, now, acc),  # 1 Cent = 10_000 µEUR
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


def account_for_token(token: str | None, kind: str = "wallet") -> str | None:
    if not token or len(token) > 200:
        return None
    with _Tx() as conn:
        row = conn.execute(
            "SELECT account_id FROM tokens WHERE token_hash = ? AND kind = ?", (_hash(token), kind)
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tokens SET last_used_at = ? WHERE token_hash = ?", (_now(), _hash(token))
        )
        return row["account_id"]


# ----- Raum -> Konto ----------------------------------------------------------
def bind_room(room: str, account_id: str, mode: str) -> bool:
    """Konto zahlt für diesen Raum. Erste Zuordnung gewinnt (True = neu gebunden)."""
    with _Tx() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO room_wallets (room, account_id, mode, created_at) VALUES (?, ?, ?, ?)",
            (room, account_id, mode, _now()),
        )
        return cur.rowcount == 1


def room_wallet(room: str) -> tuple[str, str] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT account_id, mode FROM room_wallets WHERE room = ?", (room,)
        ).fetchone()
    finally:
        conn.close()
    return (row["account_id"], row["mode"]) if row else None


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
        new = max(0, int(row["balance_ueur"]) - ueur)
        now = _now()
        conn.execute(
            "UPDATE accounts SET balance_ueur = ?, updated_at = ? WHERE id = ?", (new, now, account_id)
        )
        conn.execute(
            "INSERT INTO usage (account_id, room, mode, usd, charge_ueur, balance_after_ueur, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, room, mode, float(usd), ueur, new, now),
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
    der nicht gedeckte Teil wird in reversals.shortfall_ueur vermerkt.
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
        if debited:
            conn.execute(
                "UPDATE accounts SET balance_ueur = balance_ueur - ?, updated_at = ? WHERE id = ?",
                (debited, now, s["account_id"]),
            )
        return {"status": "reversed" if cents else "nothing", "account_id": s["account_id"],
                "cents": cents, "debited_ueur": debited, "shortfall_ueur": shortfall}

