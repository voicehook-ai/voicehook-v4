"""Magic-Link erst nach Klick einlösen (Oliver 02.10.).

Mail-Scanner (Microsoft Safe Links, Gmail-Prefetch) rufen Links per GET auf. Ein
GET darf den einmaligen Login-Token deshalb nie verbrauchen; eingelöst wird nur
per POST /api/login/verify {token, nonce} nach Klick auf "Jetzt anmelden".
Positivkontrolle: auf main (GET löst ein) ist test_get_never_consumes rot.
Kein echter Mailversand: mail.send ist gemockt (outbox).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes
from agent.billing import db, mail
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env, kein Dispatch)
from .test_billing_r4 import _link_token

WEB = Path(__file__).resolve().parents[3] / "web"


@pytest.fixture(autouse=True)
def _confirm_env(monkeypatch):
    import agent.server as srv
    monkeypatch.setattr(srv, "_HOST_LIMIT", 10_000)
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


def _request(client, outbox, email="click@x.de") -> tuple[str, str]:
    r = client.post("/api/login", json={"email": email})
    assert r.status_code == 200, r.text
    return _link_token(outbox), r.json()["login_nonce"]


def test_get_never_consumes(client, outbox):
    """Scanner-GET (auch mit Nonce und confirm=1) verbraucht nichts."""
    tok, n = _request(client, outbox)
    for params in ({"token": tok}, {"token": tok, "nonce": n}, {"token": tok, "confirm": 1}):
        client.get("/api/login/verify", params=params, follow_redirects=False)
    link = db.consume_login_link(tok, nonce=n)            # Link noch gültig
    assert link is not None and link.status == "ok"


def test_get_redirects_to_confirm_page(client, outbox):
    tok, n = _request(client, outbox)
    r = client.get("/api/login/verify", params={"token": tok, "nonce": n}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login#login=" + tok    # Token nur im Fragment
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in r.headers["cache-control"]
    r = client.get("/api/login/verify", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_post_consumes_once(client, outbox):
    tok, n = _request(client, outbox)
    r = client.post("/api/login/verify", json={"token": tok, "nonce": n})
    assert r.status_code == 200, r.text
    assert r.json()["wallet_token"].startswith("vhw_") and r.json()["email_verified"] is True
    r2 = client.post("/api/login/verify", json={"token": tok, "nonce": n})
    assert r2.status_code == 400 and r2.json()["detail"] == "invalid_or_expired"


def test_post_expired_is_400(client):
    old, n = db.create_login_link("late@x.de", now=time.time() - db.LOGIN_TTL_S - 1)
    r = client.post("/api/login/verify", json={"token": old, "nonce": n})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_or_expired"


def test_post_without_nonce_asks_first_then_confirm(client, outbox):
    """Nonce-Bindung bleibt: anderer Browser -> 409, Link unverbraucht; confirm löst ein."""
    tok, _ = _request(client, outbox)
    r = client.post("/api/login/verify", json={"token": tok})
    assert r.status_code == 409 and r.json() == {"error": "confirm_required", "email_masked": "c***@x***.de"}
    r = client.post("/api/login/verify", json={"token": tok, "confirm": True})
    assert r.status_code == 200 and r.json()["wallet_linked"] is False
    assert client.post("/api/login/verify", json={"token": tok, "confirm": True}).status_code == 400


def test_mail_link_keeps_token_out_of_query(client, outbox):
    _request(client, outbox)
    text = outbox[-1][1]["text"]
    assert "/aufladen#login=vhl_" in text and "?token=" not in text


@pytest.mark.parametrize("page", ["login.html", "aufladen.html"])
def test_pages_redeem_only_by_post_after_click(page):
    """Seiten: no-referrer, Knopf "Jetzt anmelden", Einlösen nur per POST mit Token im Body."""
    html = (WEB / page).read_text()
    assert '<meta name="referrer" content="no-referrer">' in html
    assert "Jetzt anmelden" in html and "Log in now" in html
    assert "/api/login/verify?token=" not in html
    assert "fetch('/api/login/verify', { method:'POST'" in html
