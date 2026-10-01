"""PR #93 Review-Fixes: Login-CSRF (critical), Gratis-Umgehung Normal (high),
Live-Budget in der Gratisphase mit Wallet (low). Keine echten Stripe-/Resend-Calls."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import budget, freetier
from agent.billing import db, mail, pricing
from agent.server import app
from agent.tokens import mint_invite

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env)
from .test_billing import _paid_wallet
from .test_billing_r4 import _r4_env  # noqa: F401
from .test_freetier import _run_free
from .test_wallet_worker import _account
from .test_wallet_worker import _run as _run_worker
from .test_worker import _metric


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    monkeypatch.setattr(mail, "send", lambda to, msg, **k: sent.append((to, msg)))
    return sent


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(srv, "_ensure_agent_dispatched",
                        lambda room, agent_name="voice-ai": calls.append((room, agent_name)))
    return calls


def _link(outbox) -> str:
    return re.search(r"#login=(\S+)", outbox[-1][1]["text"]).group(1)


def _wait(calls, n=1):
    for _ in range(50):
        if len(calls) >= n:
            return
        time.sleep(0.02)


def _balance(client, token) -> float | None:
    return client.get("/api/me", headers={"x-wallet-token": token}).json()["balance_eur"]


# ----- (1) Login-CSRF: PoC-Szenario aus dem Review -------------------------------
def test_csrf_case_a_unconfirmed_attacker_mail_never_takes_victim_wallet(client, outbox):
    v = _paid_wallet(client, "cs_victim", amount_cents=2000, email="victim@x.de")
    acc_v = db.account_for_token(v["wallet_token"])
    # Angreifer fordert einen Link an SEINE Adresse an und schickt ihn dem Opfer
    assert client.post("/api/login", json={"email": "evil@x.de"}).status_code == 200
    # Opfer-Browser hat keine Nonce: erst Rückfrage, dann (Opfer klickt trotzdem Ja)
    r = client.get("/api/login/verify", params={"token": _link(outbox)},
                   headers={"x-wallet-token": v["wallet_token"]})
    assert r.status_code == 409 and r.json() == {"error": "confirm_required", "email_masked": "e***@x***.de"}
    r = client.get("/api/login/verify", params={"token": _link(outbox), "confirm": 1},
                   headers={"x-wallet-token": v["wallet_token"]})
    assert r.status_code == 200
    assert r.json()["wallet_linked"] is False and r.json()["balance_eur"] == 0.0
    assert r.json()["email_masked"] == "e***@x***.de"                  # Seite zeigt, als wer
    assert db.account_for_token(r.json()["wallet_token"]) != acc_v
    # Opfer-Wallet unberührt: Token gültig, 20 EUR, Adresse nicht bestätigt
    assert db.account_for_token(v["wallet_token"]) == acc_v and _balance(client, v["wallet_token"]) == 20.0
    assert db.account(acc_v)["email_verified_at"] is None
    # Angreifer meldet sich später per Mail an: hält nichts vom Opfer
    n = client.post("/api/login", json={"email": "evil@x.de"}).json()["login_nonce"]
    r2 = client.get("/api/login/verify", params={"token": _link(outbox), "nonce": n})
    assert r2.json()["balance_eur"] == 0.0


def test_csrf_case_b_confirmed_attacker_account_never_absorbs_victim(client, outbox):
    n = client.post("/api/login", json={"email": "evil2@x.de"}).json()["login_nonce"]
    client.get("/api/login/verify", params={"token": _link(outbox), "nonce": n})  # Angreifer bestätigt
    v = _paid_wallet(client, "cs_victim2", amount_cents=2000, email="victim2@x.de")
    client.post("/api/login", json={"email": "evil2@x.de"})
    r = client.get("/api/login/verify", params={"token": _link(outbox), "confirm": 1},
                   headers={"x-wallet-token": v["wallet_token"]})
    assert r.status_code == 200 and r.json()["wallet_linked"] is False
    assert _balance(client, v["wallet_token"]) == 20.0                   # vorher: None (gelöscht)
    n = client.post("/api/login", json={"email": "evil2@x.de"}).json()["login_nonce"]
    assert client.get("/api/login/verify",
                      params={"token": _link(outbox), "nonce": n}).json()["balance_eur"] == 0.0


def test_csrf_attacker_binding_own_wallet_does_not_match_victim(client, outbox):
    att = _paid_wallet(client, "cs_att3", amount_cents=1000, email="evil3@x.de")
    v = _paid_wallet(client, "cs_victim3", amount_cents=2000, email="victim3@x.de")
    client.post("/api/login", json={"email": "evil3@x.de"}, headers={"x-wallet-token": att["wallet_token"]})
    r = client.get("/api/login/verify", params={"token": _link(outbox), "confirm": 1},
                   headers={"x-wallet-token": v["wallet_token"]})
    assert r.json()["wallet_linked"] is False
    assert _balance(client, v["wallet_token"]) == 20.0
    assert db.account(db.account_for_token(v["wallet_token"]))["email_verified_at"] is None


def test_positive_control_same_browser_requests_and_redeems_links_wallet(client, outbox):
    me = _paid_wallet(client, "cs_me", amount_cents=2000, email="me@x.de")
    acc = db.account_for_token(me["wallet_token"])
    n = client.post("/api/login", json={"email": "me@x.de"},
                    headers={"x-wallet-token": me["wallet_token"]}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link(outbox), "nonce": n},
                   headers={"x-wallet-token": me["wallet_token"]}).json()
    assert r["wallet_linked"] is True and r["balance_eur"] == 20.0
    assert db.account_for_token(r["wallet_token"]) == acc
    assert db.account_for_token(me["wallet_token"]) == acc                 # bleibt angemeldet
    assert db.account(acc)["email_verified_at"]


def test_positive_control_bound_wallet_merges_into_existing_verified_account(client, outbox):
    n = client.post("/api/login", json={"email": "two@x.de"}).json()["login_nonce"]
    first = client.get("/api/login/verify", params={"token": _link(outbox), "nonce": n}).json()
    acc = db.account_for_token(first["wallet_token"])
    w2 = _paid_wallet(client, "cs_two", amount_cents=1000, email="other@x.de")  # zweites Gerät, bezahlt
    n = client.post("/api/login", json={"email": "two@x.de"},
                    headers={"x-wallet-token": w2["wallet_token"]}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link(outbox), "nonce": n},
                   headers={"x-wallet-token": w2["wallet_token"]}).json()
    assert r["wallet_linked"] is True and db.account_for_token(r["wallet_token"]) == acc
    assert r["balance_eur"] == 10.0                                         # Guthaben überführt


def test_link_requested_without_wallet_ignores_wallet_at_redeem(client, outbox):
    """Anderes Gerät: Link ohne Wallet angefordert -> Wallet beim Einlösen bleibt getrennt."""
    w1 = _paid_wallet(client, "cs_dev", amount_cents=1000, email="dev@x.de")
    client.post("/api/login", json={"email": "dev@x.de"})
    r = client.get("/api/login/verify", params={"token": _link(outbox), "confirm": 1},
                   headers={"x-wallet-token": w1["wallet_token"]}).json()
    assert r["wallet_linked"] is False
    # Stripe-Kontakt-Mail dev@x.de: das unbestätigte Konto ist Kandidat (Regel 3) und
    # wird bestätigt, aber über den Mail-Beweis, nicht über das Browser-Token; dessen
    # alte Tokens verlieren beim ersten Bestätigen die Gültigkeit (PR #88).
    assert r["balance_eur"] == 10.0 and db.account_for_token(w1["wallet_token"]) is None


def test_migration_adds_requester_hash_to_old_login_links(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "old"))
    p = db.db_path()
    p.parent.mkdir(parents=True)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE login_links (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,"
              " created_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL)")
    c.commit()
    c.close()
    t, n = db.create_login_link("m@x.de", requester_token="vhw_x")
    assert db.consume_login_link(t) == ("confirm_required", "m@x.de", None)    # ohne Nonce
    assert db.consume_login_link(t, nonce=n) == ("ok", "m@x.de", db._hash("vhw_x"))


# ----- (2) Gratis-Umgehung Normal ----------------------------------------------
def test_invite1_on_unknown_room_mints_but_does_not_dispatch(client, dispatched):
    r = client.get("/api/token", params={"room": "ghost-normal", "identity": "u", "invite": "1"})
    assert r.status_code == 200                                             # Operator darf rein
    time.sleep(0.1)
    assert dispatched == []                                                 # aber kein Gratis-Agent
    assert freetier.room_keys("ghost-normal") is None


def test_worker_refuses_unknown_normal_room(monkeypatch):
    ctx, session = _run_free(monkeypatch, room="ghost-normal", humans=1, wait_s=0.05, live_mode=False)
    ctx.shutdown.assert_called_once_with(reason="free_room_unknown")
    session.start.assert_not_awaited()


def test_worker_unknown_normal_room_runs_when_normal_free_tier_off(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", "0")            # wie vor PR #93
    ctx, session = _run_free(monkeypatch, room="ghost-off", humans=1, wait_s=0.05, live_mode=False)
    ctx.shutdown.assert_not_called()
    session.start.assert_awaited_once()


def test_operator_join_on_invite_room_still_dispatches_and_worker_runs(client, dispatched, monkeypatch):
    r = client.post("/api/invite-room", json={"identity": "h"},
                    headers={"x-anon-id": "anon-fixfixfix-0001", "x-forwarded-for": "7.7.7.7"})
    room = r.json()["room"]
    _wait(dispatched)
    dispatched.clear()
    j = client.get("/api/token", params={"room": room, "identity": "claude", "invite": "1"})
    assert j.status_code == 200
    _wait(dispatched)
    assert dispatched == [(room, "voice-ai")]
    ctx, session = _run_free(monkeypatch, room=room, humans=1, wait_s=0.05, live_mode=False)
    ctx.shutdown.assert_not_called()
    session.start.assert_awaited_once()


def test_operator_join_on_wallet_room_dispatches(client, dispatched):
    acc = _account(1000)
    db.bind_room("paid-normal", acc, "normal")
    client.get("/api/token", params={"room": "paid-normal", "identity": "claude", "invite": "1"})
    _wait(dispatched)
    assert dispatched == [("paid-normal", "voice-ai")]


def test_hmac_invite_for_operator_minted_room_adopts_it_exempt(client, dispatched, monkeypatch):
    """call-starten: Operator mintet die Einladung mit INVITE_SECRET für einen eigenen Slug."""
    room = "drift-signal-crisp-PDM5"
    r = client.post("/api/token", json={"room": room, "identity": "oliver", "invite": mint_invite(room)})
    assert r.status_code == 200
    assert freetier.room_keys(room) == ("normal", [])                       # bekannt, nicht gezählt
    client.get("/api/token", params={"room": room, "identity": "claude", "invite": "1"})
    _wait(dispatched, 2)
    assert (room, "voice-ai") in dispatched
    ctx, session = _run_free(monkeypatch, room=room, humans=1, wait_s=0.05, live_mode=False)
    ctx.shutdown.assert_not_called()
    session.start.assert_awaited_once()


def test_hmac_invite_never_overwrites_counted_free_room(client):
    keys = freetier.identity_keys("anon-fixfixfix-0002", "6.6.6.6")
    freetier.register_room("counted-room", "normal", keys)
    client.post("/api/token", json={"room": "counted-room", "identity": "g", "invite": mint_invite("counted-room")})
    assert freetier.room_keys("counted-room") == ("normal", keys)          # Zählung bleibt


# ----- (3) Live mit Wallet in der Gratisphase: Monatsbudget zählt -----------------
_RT = dict(input_tokens=1000, output_tokens=500, input_token_details=None, output_token_details=None)


def test_live_free_phase_with_wallet_counts_month_budget(monkeypatch):
    acc = _account(1000)
    db.bind_room("live-mix", acc, "live")
    freetier.register_room("live-mix", "live", freetier.identity_keys("anon-fixfixfix-0003", "5.5.5.5"))
    ctx, _, _ = _run_worker(monkeypatch, live_mode=True, room="live-mix",
                            metrics=[_metric("RealtimeModelMetrics", **_RT)])
    usd = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert budget.spent_usd() == pytest.approx(usd)                        # Gratis-Teil zählt
    assert db.balance_ueur(acc) == 10_000_000                              # Wallet unberührt
    ctx.shutdown.assert_not_called()


def test_live_budget_exhausted_paid_room_skips_free_and_charges_wallet(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "0.001")
    budget.add_usd(0.01)
    acc = _account(1000)
    db.bind_room("live-mix2", acc, "live")
    keys = freetier.identity_keys("anon-fixfixfix-0004", "4.4.4.5")
    freetier.register_room("live-mix2", "live", keys)
    ctx, _, _ = _run_worker(monkeypatch, live_mode=True, room="live-mix2",
                            metrics=[_metric("RealtimeModelMetrics", **_RT)])
    usd = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert db.balance_ueur(acc) == 10_000_000 - round(usd * pricing.DEFAULT_USD_EUR * 1.5 * 1.19 * 1e6)
    assert freetier.used_seconds(keys, "live") == 0
    ctx.shutdown.assert_not_called()                                       # zahlender Kunde läuft
