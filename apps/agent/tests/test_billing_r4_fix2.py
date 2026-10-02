"""PR #93 Re-Review: Rest-Login-CSRF bei Browser ohne Wallet (medium), Login-
Ratenlimits (IPv6 /64, LRU, Mail-Fehler, Opfer-Sperre), Recovery-Codes beim Login.
Keine echten Resend-Calls."""

from __future__ import annotations

import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes
from agent.billing import db, mail
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env)
from .test_billing import _paid_wallet
from .test_billing_r4 import _r4_env  # noqa: F401


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    monkeypatch.setattr(mail, "send", lambda to, msg, **k: sent.append((to, msg)))
    return sent


def _link(outbox) -> str:
    return re.search(r"#login=(\S+)", outbox[-1][1]["text"]).group(1)


def _request(client, email, wallet=None, ip=None) -> str:
    headers = {}
    if wallet:
        headers["x-wallet-token"] = wallet
    if ip:
        headers["x-forwarded-for"] = ip
    r = client.post("/api/login", json={"email": email}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["login_nonce"]


def _verify(client, token, *, nonce=None, confirm=False, wallet=None):
    params = {"token": token}
    if nonce:
        params["nonce"] = nonce
    if confirm:
        params["confirm"] = 1
    headers = {"x-wallet-token": wallet} if wallet else {}
    return client.post("/api/login/verify", json=params, headers=headers)


# ----- (1) Rest-Login-CSRF: Browser ohne Wallet ------------------------------------
def test_residual_login_csrf_fresh_browser_gets_attacker_account(client, outbox):
    """Opfer-Browser OHNE Wallet (Neukunde) öffnet einen Link, den der Angreifer an
    SEINE Adresse angefordert hat. Früher: stilles Login ins Angreiferkonto (zahlt
    das Opfer danach auf, landet das Geld beim Angreifer). Jetzt: ohne login_nonce
    keine Anmeldung, nur Rückfrage mit maskierter Adresse; der Link bleibt unverbraucht."""
    _request(client, "evil3@x.de")                              # Angreifer-Browser
    tok = _link(outbox)
    accounts_before = db.connect().execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    r = _verify(client, tok)                                    # Opfer-Browser, ohne Nonce
    assert r.status_code == 409
    assert r.json() == {"error": "confirm_required", "email_masked": "e***@x***.de"}
    assert "wallet_token" not in r.json()                       # kein Token fürs Angreiferkonto
    assert db.connect().execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == accounts_before
    # Falsche Nonce (z. B. eine eigene alte) zählt genauso wenig
    assert _verify(client, tok, nonce="vhn_" + "x" * 43).status_code == 409
    # Link unverbraucht: der Angreifer selbst kann sich damit noch anmelden
    row = db.connect().execute("SELECT used_at FROM login_links").fetchone()
    assert row["used_at"] is None


def test_positive_control_same_device_with_nonce_logs_in_without_question(client, outbox):
    nonce = _request(client, "fresh@x.de")
    r = _verify(client, _link(outbox), nonce=nonce)
    assert r.status_code == 200 and r.json()["email_masked"] == "f***@x***.de"
    acc = db.account_for_token(r.json()["wallet_token"])
    assert db.account(acc)["email"] == "fresh@x.de" and db.account(acc)["email_verified_at"]
    assert _verify(client, _link(outbox), nonce=nonce).status_code == 400   # einmalig


def test_positive_control_other_device_with_confirm_logs_in_but_never_links_wallet(client, outbox):
    """Link auf Gerät A (mit Wallet) angefordert, auf Gerät B geöffnet: Rückfrage,
    nach Ja (confirm=1) Login. Selbst wenn B das anfordernde Wallet-Token mitschickt,
    wird ohne Nonce nie ein Wallet verknüpft."""
    me = _paid_wallet(client, "cs_conf", amount_cents=1000, email="other@x.de")
    _request(client, "dev@x.de", wallet=me["wallet_token"])
    tok = _link(outbox)
    assert _verify(client, tok, wallet=me["wallet_token"]).status_code == 409
    r = _verify(client, tok, confirm=True, wallet=me["wallet_token"])
    assert r.status_code == 200 and r.json()["wallet_linked"] is False
    acc_new = db.account_for_token(r.json()["wallet_token"])
    acc_me = db.account_for_token(me["wallet_token"])
    assert acc_new != acc_me and r.json()["balance_eur"] == 0.0
    assert db.account(acc_me)["email_verified_at"] is None                 # Wallet unberührt
    assert client.get("/api/me", headers={"x-wallet-token": me["wallet_token"]}).json()["balance_eur"] == 10.0
    assert _verify(client, tok, confirm=True).status_code == 400           # jetzt verbraucht


def test_wrong_nonce_does_not_consume_link_right_nonce_still_works(client, outbox):
    nonce = _request(client, "wn@x.de")
    _request(client, "wn2@x.de")                                            # andere Nonce
    tok_first = re.search(r"#login=(\S+)", outbox[0][1]["text"]).group(1)
    other = client.post("/api/login", json={"email": "x@x.de"}).json()["login_nonce"]
    assert _verify(client, tok_first, nonce=other).status_code == 409
    assert _verify(client, tok_first, nonce=nonce).status_code == 200


def test_legacy_link_without_nonce_needs_confirm(client, outbox):
    """Links aus der Zeit vor dem Deploy haben keine nonce_hash: nur mit confirm."""
    tok, _ = db.create_login_link("legacy@x.de", requester_token="vhw_old")
    with db._Tx() as conn:
        conn.execute("UPDATE login_links SET nonce_hash = NULL")
    assert _verify(client, tok, nonce="vhn_irgendwas").status_code == 409
    r = _verify(client, tok, confirm=True, wallet="vhw_old")
    assert r.status_code == 200 and r.json()["wallet_linked"] is False


def test_migration_adds_nonce_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "old"))
    p = db.db_path()
    p.parent.mkdir(parents=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE login_links (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,"
              " created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL, requester_hash TEXT)")
    c.commit()
    c.close()
    t, n = db.create_login_link("m@x.de")
    assert db.consume_login_link(t, nonce=n) == ("ok", "m@x.de", None)


# ----- (2) Ratenlimit je IP-Bucket, LRU, Mail-Fehler ----------------------------------
def test_ipv6_rotation_within_one_64_hits_ip_limit(client, outbox):
    codes = [client.post("/api/login", json={"email": f"spam{i}@x.de"},
                         headers={"x-forwarded-for": f"2001:db8:1:2::{i + 1:x}"}).status_code
             for i in range(8)]
    assert codes == [200] * 5 + [429] * 3                                   # ein /64 = eine IP
    # Positivkontrolle: ein anderes /64 hat sein eigenes Kontingent
    assert client.post("/api/login", json={"email": "spam9@x.de"},
                       headers={"x-forwarded-for": "2001:db8:1:3::1"}).status_code == 200


def test_lru_flood_does_not_reset_recent_limits(client, outbox, monkeypatch):
    monkeypatch.setattr(billing_routes, "LOGIN_HITS_MAX", 100)
    for i in range(90):                                                     # alte Schlüssel
        billing_routes._login_rate_take([(f"ip:flood-a-{i}", 5, 600)])
    for _ in range(3):
        _request(client, "victim@x.de", ip="9.9.9.9")
    assert client.post("/api/login", json={"email": "victim@x.de"},
                       headers={"x-forwarded-for": "9.9.9.9"}).status_code == 429
    for i in range(20):                                                     # Überlauf
        billing_routes._login_rate_take([(f"ip:flood-b-{i}", 5, 600)])
    assert len(billing_routes._LOGIN_HITS) <= 100
    assert "ip:flood-a-0" not in billing_routes._LOGIN_HITS                 # Älteste verdrängt
    assert client.post("/api/login", json={"email": "victim@x.de"},
                       headers={"x-forwarded-for": "9.9.9.9"}).status_code == 429   # vorher: clear() -> 200


def test_mail_failure_does_not_count_against_limits(client, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    fail = {"on": True}
    sent = []

    def _send(to, msg, **k):
        if fail["on"]:
            raise mail.MailError("resend 500")
        sent.append(to)

    monkeypatch.setattr(mail, "send", _send)
    for _ in range(6):
        assert client.post("/api/login", json={"email": "mf@x.de"},
                           headers={"x-forwarded-for": "5.5.5.5"}).status_code == 502
    fail["on"] = False
    assert client.post("/api/login", json={"email": "mf@x.de"},
                       headers={"x-forwarded-for": "5.5.5.5"}).status_code == 200
    assert sent == ["mf@x.de"]


def test_rejected_request_consumes_no_quota(client, outbox):
    """Abgelehnt (Adresse+IP voll) zählt nicht gegen das IP-Limit."""
    for _ in range(3):
        _request(client, "a@x.de", ip="4.4.4.4")
    for _ in range(3):
        assert client.post("/api/login", json={"email": "a@x.de"},
                           headers={"x-forwarded-for": "4.4.4.4"}).status_code == 429
    for i in range(2):
        _request(client, f"b{i}@x.de", ip="4.4.4.4")                       # IP: 3 + 2 = 5
    assert client.post("/api/login", json={"email": "c@x.de"},
                       headers={"x-forwarded-for": "4.4.4.4"}).status_code == 429


# ----- (3) Opfer-Sperre ------------------------------------------------------------------
def test_victim_not_locked_out_by_three_foreign_requests(client, outbox):
    for i in range(3):
        _request(client, "victim@x.de", ip=f"9.9.9.{i}")
    assert client.post("/api/login", json={"email": "victim@x.de"},
                       headers={"x-forwarded-for": "1.2.3.4"}).status_code == 200


def test_per_address_hourly_cap(client, outbox):
    for i in range(10):
        _request(client, "cap@x.de", ip=f"10.0.{i}.1")
    assert client.post("/api/login", json={"email": "cap@x.de"},
                       headers={"x-forwarded-for": "10.0.99.1"}).status_code == 429
    assert client.post("/api/login", json={"email": "other@x.de"},     # Positivkontrolle
                       headers={"x-forwarded-for": "10.0.99.1"}).status_code == 200


def test_per_address_and_ip_limit(client, outbox):
    for _ in range(3):
        _request(client, "ai@x.de", ip="3.3.3.3")
    assert client.post("/api/login", json={"email": "ai@x.de"},
                       headers={"x-forwarded-for": "3.3.3.3"}).status_code == 429


# ----- (4) Recovery-Codes beim Login widerrufen ---------------------------------------
def test_login_revokes_older_recovery_codes(client, outbox):
    w = _paid_wallet(client, "cs_rc", amount_cents=1000, email="other@x.de")
    old_code = w["recovery_url"].split("#r=")[1]
    n = _request(client, "rc@x.de", wallet=w["wallet_token"])
    r1 = _verify(client, _link(outbox), nonce=n, wallet=w["wallet_token"]).json()
    assert r1["wallet_linked"] is True
    n = _request(client, "rc@x.de")
    r2 = _verify(client, _link(outbox), nonce=n).json()
    code1 = r1["recovery_url"].split("#r=")[1]
    code2 = r2["recovery_url"].split("#r=")[1]
    assert client.post("/api/wallet/recover", json={"code": old_code}).status_code == 404
    assert client.post("/api/wallet/recover", json={"code": code1}).status_code == 404
    assert client.post("/api/wallet/recover", json={"code": code2}).status_code == 200   # neuester gilt
    # Wallet-Tokens anderer Geräte bleiben angemeldet
    assert db.account_for_token(w["wallet_token"]) == db.account_for_token(r2["wallet_token"])


def test_parallel_replay_only_one_wins(client, outbox):
    import concurrent.futures as cf
    n = _request(client, "race@x.de")
    t = _link(outbox)
    with cf.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda _: _verify(client, t, nonce=n).status_code, range(8)))
    assert codes.count(200) == 1 and codes.count(400) == 7


def test_expired_link_is_400_even_with_confirm(client, outbox):
    old, n = db.create_login_link("exp@x.de", now=time.time() - db.LOGIN_TTL_S - 1)
    assert _verify(client, old, nonce=n).status_code == 400
    assert _verify(client, old, confirm=True).status_code == 400
