"""E-Mail-Login per Magic-Link über Resend (feat/resend-login, 02.10.).

Ergänzt test_billing_r4*.py: Sending-Key-Env, Resend-Request-Form, Link einmalig und
nach 15 min tot (mit Positivkontrolle kurz vor Ablauf), Limit je Adresse über viele
IPs, Adresse nie im Log. Keine echten Resend-Calls."""

from __future__ import annotations

import io
import json
import logging
import re
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes
from agent.billing import db, mail
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env)
from .test_billing_r4 import _r4_env  # noqa: F401

MAIL = "Geheim.Person@Example.ORG"
NORM = MAIL.lower()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("RESEND_SENDING_API_KEY", "re_test_sending_do_not_use")
    monkeypatch.setattr(mail, "send", lambda to, msg, **k: sent.append((to, msg)))
    return sent


def _link(outbox) -> str:
    return re.search(r"#login=(\S+)", outbox[-1][1]["text"]).group(1)


def _login(client, email=MAIL, ip="4.4.4.4"):
    return client.post("/api/login", json={"email": email}, headers={"x-forwarded-for": ip})


# ----- Env: Sending-Key -----------------------------------------------------------
def test_sending_key_env_enables_login(client, outbox):
    assert client.get("/api/auth/providers").json()["email"] is True
    assert _login(client).status_code == 200


def test_sending_key_wins_over_legacy_env(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_legacy")
    monkeypatch.setenv("RESEND_SENDING_API_KEY", "re_sending")
    assert mail.api_key() == "re_sending"
    monkeypatch.setenv("RESEND_SENDING_API_KEY", "  ")
    assert mail.api_key() == "re_legacy"                           # Fallback für alte .env
    monkeypatch.delenv("RESEND_API_KEY")
    assert mail.api_key() == "" and mail.configured() is False


def test_no_key_at_all_is_503(client):
    r = _login(client)
    assert r.status_code == 503 and r.json()["detail"] == "login_unavailable"
    assert client.get("/api/auth/providers").json()["email"] is False


# ----- Resend-Request -------------------------------------------------------------
class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_posts_resend_payload(monkeypatch):
    monkeypatch.setenv("RESEND_SENDING_API_KEY", "re_sending")
    monkeypatch.delenv("MAIL_FROM", raising=False)
    seen = {}

    def _open(req, timeout):
        seen.update(url=req.full_url, method=req.get_method(), auth=req.get_header("Authorization"), ua=req.get_header("User-agent"),
                    body=json.loads(req.data), timeout=timeout)
        return _Resp(b'{"id":"x"}')

    monkeypatch.setattr(mail.urllib.request, "urlopen", _open)
    mail.send("a@b.de", mail.login_message("https://vh.test/aufladen#login=vhl_x", 15))
    assert seen["url"] == "https://api.resend.com/emails" and seen["method"] == "POST"
    assert seen["auth"] == "Bearer re_sending"
    # Resend steht hinter Cloudflare: Default-UA "Python-urllib/x" -> 403 "error code: 1010"
    # (gemessen 02.10. mit echtem Sending-Key; eigener UA -> normale API-Antwort).
    assert seen["ua"] and not seen["ua"].lower().startswith("python-urllib")
    assert seen["body"]["from"] == "voicehook <login@voicehook.ai>"
    assert seen["body"]["to"] == ["a@b.de"]
    assert "gültig 15 Minuten" in seen["body"]["text"] and "vhl_x" in seen["body"]["html"]


def test_send_error_names_status_not_recipient(monkeypatch):
    monkeypatch.setenv("RESEND_SENDING_API_KEY", "re_sending")

    def _open(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"to":"x"}'))

    monkeypatch.setattr(mail.urllib.request, "urlopen", _open)
    with pytest.raises(mail.MailError) as e:
        mail.send(NORM, mail.login_message("https://x", 15))
    assert str(e.value) == "resend 403" and "@" not in str(e.value)


# ----- Token: einmalig, 15 min ----------------------------------------------------
def test_positive_control_link_logs_in_once(client, outbox):
    nonce = _login(client).json()["login_nonce"]
    assert outbox[-1][0] == NORM                                    # normalisiert verschickt
    tok = _link(outbox)
    r = client.post("/api/login/verify", json={"token": tok, "nonce": nonce})
    assert r.status_code == 200 and r.json()["wallet_token"].startswith("vhw_")
    again = client.post("/api/login/verify", json={"token": tok, "nonce": nonce})
    assert again.status_code == 400                                 # einmalig


def test_ttl_is_15_minutes_and_boundary(client):
    assert db.LOGIN_TTL_S == 15 * 60
    fresh, n1 = db.create_login_link("edge@x.de", now=time.time() - db.LOGIN_TTL_S + 30)
    assert client.post("/api/login/verify", json={"token": fresh, "nonce": n1}).status_code == 200
    old, n2 = db.create_login_link("edge@x.de", now=time.time() - db.LOGIN_TTL_S - 1)
    assert client.post("/api/login/verify", json={"token": old, "nonce": n2}).status_code == 400


def test_db_stores_only_hash_of_token(client, outbox):
    _login(client)
    tok = _link(outbox)
    rows = [tuple(r) for r in db.connect().execute("SELECT * FROM login_links").fetchall()]
    assert rows                                                     # Positivkontrolle: Zeile existiert
    assert not any(tok in str(c) for r in rows for c in r)          # Klartext-Token nirgends


# ----- Rate-Limit -----------------------------------------------------------------
def test_rate_limit_per_mail_across_many_ips(client, outbox):
    limit = billing_routes.LOGIN_MAIL_LIMIT
    for i in range(limit):
        assert _login(client, ip=f"3.3.{i}.1").status_code == 200
    assert _login(client, ip="3.3.200.1").status_code == 429       # je Adresse, IP egal
    assert _login(client, email="other@x.de", ip="3.3.201.1").status_code == 200   # Gegenprobe


def test_rate_limit_per_ip_across_mails(client, outbox):
    for i in range(billing_routes.LOGIN_IP_LIMIT):
        assert _login(client, email=f"m{i}@x.de", ip="2.2.2.2").status_code == 200
    assert _login(client, email="m99@x.de", ip="2.2.2.2").status_code == 429
    assert _login(client, email="m99@x.de", ip="2.2.2.3").status_code == 200       # Gegenprobe


# ----- Datenschutz: Adresse nie im Log --------------------------------------------
def test_email_never_logged(client, outbox, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    nonce = _login(client).json()["login_nonce"]
    client.post("/api/login/verify", json={"token": _link(outbox), "nonce": nonce})
    for _ in range(5):
        _login(client)                                              # bis ins Rate-Limit
    monkeypatch.setattr(mail, "send", lambda *a, **k: (_ for _ in ()).throw(mail.MailError("resend 500")))
    billing_routes._LOGIN_HITS.clear()
    assert _login(client).status_code == 502
    text = caplog.text.lower()
    assert "[login]" in text                                        # Positivkontrolle: es wurde geloggt
    assert NORM not in text and "geheim.person" not in text and "example.org" not in text
