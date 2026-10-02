"""Abmelden (POST /api/logout) und Access-Log ohne Geheimnisse (Oliver 02.10.)."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes, freetier, logredact
from agent.billing import db, mail
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env, kein Dispatch)
from .test_billing_r4 import ANON, H, _link_token


@pytest.fixture(autouse=True)
def _logout_env(monkeypatch):
    import agent.server as srv
    monkeypatch.setattr(srv, "_HOST_LIMIT", 10_000)
    for k in ("VH_LOW_BALANCE_WARN_SECONDS", "VH_FREE_TICK_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    billing_routes._LOGIN_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    monkeypatch.setattr(mail, "send", lambda to, msg, **k: sent.append((to, msg)))
    return sent


def _login(client, email, outbox):
    n = client.post("/api/login", json={"email": email}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n})
    assert r.status_code == 200, r.text
    return r.json()["wallet_token"]


def test_logout_revokes_exactly_this_token(client, outbox):
    a = _login(client, "out@x.de", outbox)            # Gerät A
    b = _login(client, "out@x.de", outbox)            # Gerät B, gleiches Konto
    acc = db.account_for_token(a)
    assert acc is not None and db.account_for_token(b) == acc          # Positivkontrolle
    me = client.get("/api/me", headers={**H, "x-wallet-token": a}).json()
    assert me["email_verified"] is True and me["email_masked"]

    r = client.post("/api/logout", headers={"x-wallet-token": a})
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": True}

    me = client.get("/api/me", headers={**H, "x-wallet-token": a}).json()
    assert me["email_masked"] is None and me["email_verified"] is None and me["balance_eur"] is None
    assert db.account_for_token(a) is None
    # anderes Gerät desselben Kontos bleibt angemeldet
    me_b = client.get("/api/me", headers={**H, "x-wallet-token": b}).json()
    assert me_b["email_verified"] is True and db.account_for_token(b) == acc
    # idempotent: zweites Abmelden mit demselben Token ist ein 200-no-op
    r2 = client.post("/api/logout", headers={"x-wallet-token": a})
    assert r2.status_code == 200 and r2.json() == {"ok": True, "revoked": False}


def test_logout_without_token_is_noop(client):
    r = client.post("/api/logout")
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": False}
    r = client.post("/api/logout", headers={"x-wallet-token": "vhw_unbekannt"})
    assert r.status_code == 200 and r.json()["revoked"] is False


def test_logged_out_token_cannot_pay_for_calls(client, outbox):
    """Nach dem Abmelden zahlt das Token nichts mehr: Gerät gilt als Gast (Gratis-Logik)."""
    from .test_billing import _paid_wallet
    _paid_wallet(client, "cs_lo", amount_cents=1000, email="lo@x.de")
    tok = _login(client, "lo@x.de", outbox)
    freetier.add_ueur(freetier.identity_keys(ANON, "8.8.8.8"), 1_000_000)   # Gratis heute leer
    ok = client.post("/api/host-call", json={"identity": "u"}, headers={**H, "x-wallet-token": tok})
    assert ok.status_code == 200                                            # Positivkontrolle: Wallet zahlt
    assert client.post("/api/logout", headers={"x-wallet-token": tok}).status_code == 200
    r = client.post("/api/host-call", json={"identity": "u"}, headers={**H, "x-wallet-token": tok})
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"
    r = client.post("/api/invite-room", json={"identity": "h"}, headers={**H, "x-wallet-token": tok})
    assert r.status_code == 402


def test_logout_keeps_recovery_code(client, outbox):
    n = client.post("/api/login", json={"email": "rc@x.de"}).json()["login_nonce"]
    v = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n}).json()
    client.post("/api/logout", headers={"x-wallet-token": v["wallet_token"]})
    code = v["recovery_url"].split("#r=")[1]
    assert db.account_for_token(code, "recovery") is not None               # Wiederherstellen geht weiter


# ----- Access-Log ------------------------------------------------------------------
def test_redact_query_replaces_only_secret_values():
    f = logredact.redact_query
    assert f("/api/auth/github/callback?code=abc123&state=st_xyz") == "/api/auth/github/callback?code=…&state=…"
    assert f("/api/login/verify?token=vhl_SECRET&nonce=n0nce&confirm=1") == "/api/login/verify?token=…&nonce=…&confirm=1"
    assert f("/r/abc-def?invite=INV.sig&op_invite=OP.sig") == "/r/abc-def?invite=…&op_invite=…"
    assert f("/api/me?x=1&mycode=keep") == "/api/me?x=1&mycode=keep"              # nur exakte Namen
    assert f("/api/free/remaining") == "/api/free/remaining"


def test_uvicorn_access_log_has_no_secrets(caplog):
    import agent.server  # noqa: F401  (installiert den Filter beim Import)
    lg = logging.getLogger("uvicorn.access")
    assert any(isinstance(x, logredact.AccessLogRedactor) for x in lg.filters)
    logredact.install()                                                     # idempotent
    assert sum(isinstance(x, logredact.AccessLogRedactor) for x in lg.filters) == 1
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET",
                "/api/login/verify?token=vhl_TOPSECRET&nonce=NONCE42", "1.1", 200)
        lg.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET",
                "/api/auth/google/callback?state=STATE9&code=CODE7", "1.1", 302)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in ("vhl_TOPSECRET", "NONCE42", "STATE9", "CODE7"):
        assert secret not in text
    assert "/api/login/verify?token=…&nonce=…" in text and " 200" in text
