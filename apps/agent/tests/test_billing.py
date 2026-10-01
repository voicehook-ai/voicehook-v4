"""Aufladen + Wallet: Stripe-Webhook (Signatur, Idempotenz), Abbuchung, Gating.

Stripe wird nie echt aufgerufen: Checkout per Monkeypatch, Webhook mit selbst
signierten Fixtures (gleiches HMAC-Verfahren wie Stripe).
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes
from agent.billing import db, pricing, stripe_api
from agent.server import app

WH_SECRET = "whsec_test_do_not_use"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WH_SECRET)
    monkeypatch.setenv("VOICEHOOK_PUBLIC_URL", "https://vh.test")
    monkeypatch.setenv("INVITE_SECRET", "test-secret-do-not-use")
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    for k in ("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "VOICEHOOK_REQUIRE_CREDITS_LIVE",
              "VOICEHOOK_PRICE_FACTOR_NORMAL", "VOICEHOOK_PRICE_FACTOR_LIVE",
              "VOICEHOOK_VAT_RATE", "VOICEHOOK_USD_EUR", "VOICEHOOK_TOPUP_AMOUNTS_EUR",
              "VOICEHOOK_TOPUP_MIN_EUR", "VOICEHOOK_TOPUP_MAX_EUR"):
        monkeypatch.delenv(k, raising=False)
    # Kein Test darf LiveKit erreichen
    import agent.server as srv
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    srv._HOST_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _event(session_id="cs_test_1", email="Kunde@Example.com", amount_cents=2000, *,
           etype="checkout.session.completed", paid=True, currency="eur", metadata=None):
    return {
        "id": "evt_" + session_id, "type": etype,
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "amount_total": amount_cents,
            "currency": currency, "payment_status": "paid" if paid else "unpaid",
            "customer_details": {"email": email}, "metadata": metadata or {},
        }},
    }


def _post_webhook(client, event, *, secret=WH_SECRET, ts=None, sig=None):
    body = json.dumps(event).encode()
    header = sig or stripe_api.sign(body, secret, int(ts or time.time()))
    return client.post("/api/stripe/webhook", content=body,
                       headers={"stripe-signature": header, "content-type": "application/json"})


def _paid_wallet(client, session_id="cs_test_w", amount_cents=1000):
    assert _post_webhook(client, _event(session_id, amount_cents=amount_cents)).status_code == 200
    r = client.post("/api/wallet/claim", json={"session_id": session_id})
    assert r.status_code == 200, r.text
    return r.json()


# ----- Webhook: Signatur + Idempotenz -------------------------------------------
def test_webhook_valid_signature_credits_once(client):
    r1 = _post_webhook(client, _event())
    assert r1.status_code == 200 and r1.json()["credited"] is True   # Positivkontrolle
    r2 = _post_webhook(client, _event())                              # Wiederzustellung
    assert r2.status_code == 200 and r2.json() == {"received": True, "credited": False, "duplicate": True}
    w = client.post("/api/wallet/claim", json={"session_id": "cs_test_1"}).json()
    assert w["balance_eur"] == 20.0                                   # nur EINMAL 20 EUR
    assert w["email"] == "kunde@example.com"


def test_webhook_wrong_signature_is_400_and_credits_nothing(client):
    r = _post_webhook(client, _event(), secret="whsec_wrong")
    assert r.status_code == 400
    assert client.post("/api/wallet/claim", json={"session_id": "cs_test_1"}).status_code == 202


def test_webhook_tampered_body_is_400(client):
    good = _event(amount_cents=1000)
    body = json.dumps(good).encode()
    sig = stripe_api.sign(body, WH_SECRET, int(time.time()))
    evil = json.dumps(_event(amount_cents=999900)).encode()
    r = client.post("/api/stripe/webhook", content=evil, headers={"stripe-signature": sig})
    assert r.status_code == 400


@pytest.mark.parametrize("header", ["", "garbage", "t=1", "v1=abc"])
def test_webhook_malformed_header_is_400(client, header):
    r = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": header})
    assert r.status_code == 400


def test_webhook_old_timestamp_is_400(client):
    assert _post_webhook(client, _event(), ts=time.time() - 3600).status_code == 400


def test_webhook_without_secret_is_503(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET")
    assert _post_webhook(client, _event()).status_code == 503


def test_webhook_ignores_unpaid_other_currency_and_other_types(client):
    assert _post_webhook(client, _event("cs_a", paid=False)).json()["ignored"] == "not_paid"
    assert _post_webhook(client, _event("cs_b", currency="usd")).json()["ignored"] == "currency"
    assert _post_webhook(client, _event("cs_c", etype="charge.refunded")).json()["ignored"] == "charge.refunded"
    for sid in ("cs_a", "cs_b", "cs_c"):
        assert client.post("/api/wallet/claim", json={"session_id": sid}).status_code == 202


def test_async_payment_succeeded_credits(client):
    r = _post_webhook(client, _event("cs_sepa", etype="checkout.session.async_payment_succeeded"))
    assert r.json()["credited"] is True


def test_concurrent_duplicate_record_is_idempotent():
    assert db.record_stripe_session("cs_x", "a@b.c", 1000) is True
    assert db.record_stripe_session("cs_x", "a@b.c", 1000) is False
    acc = db.claim_session("cs_x")[1]
    assert db.balance_ueur(acc) == 10 * 1_000_000


def test_topup_from_existing_wallet_goes_to_same_account(client):
    w = _paid_wallet(client)
    acc = db.account_for_token(w["wallet_token"])
    _post_webhook(client, _event("cs_more", email="other@example.com", amount_cents=500,
                                 metadata={"vh_account": acc}))
    r = client.get("/api/wallet", headers={"x-wallet-token": w["wallet_token"]})
    assert r.json()["balance_eur"] == 15.0


# ----- Claim / Wallet / Recover ---------------------------------------------------
def test_claim_pending_then_once(client):
    assert client.post("/api/wallet/claim", json={"session_id": "cs_test_1"}).status_code == 202
    _post_webhook(client, _event())
    first = client.post("/api/wallet/claim", json={"session_id": "cs_test_1"})
    assert first.status_code == 200
    body = first.json()
    assert body["wallet_token"].startswith("vhw_")
    assert body["recovery_url"].startswith("https://vh.test/aufladen#r=vhr_")
    assert client.post("/api/wallet/claim", json={"session_id": "cs_test_1"}).status_code == 409


def test_wallet_balance_requires_token(client):
    w = _paid_wallet(client)
    assert client.get("/api/wallet").status_code == 401
    assert client.get("/api/wallet", headers={"x-wallet-token": "vhw_falsch"}).status_code == 401
    ok = client.get("/api/wallet", headers={"x-wallet-token": w["wallet_token"]})
    assert ok.status_code == 200 and ok.json()["balance_eur"] == 10.0   # Positivkontrolle


def test_recovery_code_is_not_a_wallet_token_and_vice_versa(client):
    w = _paid_wallet(client)
    code = w["recovery_url"].split("#r=", 1)[1]
    assert client.get("/api/wallet", headers={"x-wallet-token": code}).status_code == 401
    assert client.post("/api/wallet/recover", json={"code": w["wallet_token"]}).status_code == 404
    r = client.post("/api/wallet/recover", json={"code": code})
    assert r.status_code == 200
    new = r.json()["wallet_token"]
    assert new != w["wallet_token"]
    assert client.get("/api/wallet", headers={"x-wallet-token": new}).json()["balance_eur"] == 10.0


def test_tokens_are_stored_hashed():
    acc = db.claim_session("missing")[1]
    assert acc is None
    db.record_stripe_session("cs_h", "h@x.y", 1000)
    acc = db.claim_session("cs_h")[1]
    tok = db.issue_token(acc)
    raw = db.db_path().read_bytes()
    assert tok.encode() not in raw


# ----- Checkout (Stripe gemockt) -------------------------------------------------
def test_checkout_builds_stripe_session(client, monkeypatch):
    seen = {}

    def fake_create(params, **_):
        seen.update(params)
        return {"id": "cs_new", "url": "https://checkout.stripe.test/cs_new"}

    monkeypatch.setattr(stripe_api, "create_checkout_session", fake_create)
    r = client.post("/api/checkout", json={"amount_eur": 20})
    assert r.status_code == 200
    assert r.json() == {"url": "https://checkout.stripe.test/cs_new", "session_id": "cs_new"}
    li = seen["line_items"][0]["price_data"]
    assert li["currency"] == "eur" and li["unit_amount"] == 2000
    assert seen["mode"] == "payment"
    assert seen["success_url"] == "https://vh.test/aufladen?session_id={CHECKOUT_SESSION_ID}"
    assert "customer_email" not in seen      # E-Mail kommt aus Stripe Checkout


def test_checkout_amount_bounds_and_minimum_10(client, monkeypatch):
    monkeypatch.setattr(stripe_api, "create_checkout_session", lambda p, **_: {"id": "x", "url": "u"})
    assert client.post("/api/checkout", json={"amount_eur": 9}).status_code == 400
    assert client.post("/api/checkout", json={"amount_eur": 201}).status_code == 400
    assert client.post("/api/checkout", json={"amount_eur": 10}).status_code == 200
    monkeypatch.setenv("VOICEHOOK_TOPUP_MIN_EUR", "5")   # unter 10 nie erlaubt
    assert pricing.min_topup_eur() == 10


def test_checkout_503_without_stripe_key(client, monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert client.post("/api/checkout", json={"amount_eur": 20}).status_code == 503
    assert client.get("/api/billing/config").json()["checkout_available"] is False


def test_checkout_form_encoding_matches_stripe_nesting():
    enc = stripe_api.form_encode({"line_items": [{"price_data": {"unit_amount": 1000}}], "mode": "payment"})
    assert enc == b"line_items%5B0%5D%5Bprice_data%5D%5Bunit_amount%5D=1000&mode=payment"


def test_config_amounts_from_env(client, monkeypatch):
    assert client.get("/api/billing/config").json()["amounts_eur"] == [10, 20, 50]
    monkeypatch.setenv("VOICEHOOK_TOPUP_AMOUNTS_EUR", "5,25,100")
    assert client.get("/api/billing/config").json()["amounts_eur"] == [25, 100]


# ----- Preis: Faktor 3 / 1,5, MwSt, Kurs ---------------------------------------
def test_charge_factor_normal_3_live_1_5_plus_vat():
    usd = 1.0
    base = usd * pricing.DEFAULT_USD_EUR * 1.19 * 1_000_000
    assert pricing.charge_ueur(usd, "normal") == round(base * 3)
    assert pricing.charge_ueur(usd, "live") == round(base * 1.5)


def test_charge_env_overrides_and_broken_values_fall_back(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_USD_EUR", "1")
    monkeypatch.setenv("VOICEHOOK_VAT_RATE", "0")
    assert pricing.charge_ueur(1.0, "normal") == 3_000_000
    monkeypatch.setenv("VOICEHOOK_PRICE_FACTOR_NORMAL", "0")      # nie gratis
    assert pricing.factor("normal") == 3.0
    monkeypatch.setenv("VOICEHOOK_PRICE_FACTOR_LIVE", "kaputt")
    assert pricing.factor("live") == 1.5


def test_db_charge_clamps_and_logs_usage():
    db.record_stripe_session("cs_c", "c@x.y", 1000)
    acc = db.claim_session("cs_c")[1]
    assert db.charge(acc, 4_000_000, room="r", mode="normal", usd=0.1) == 6_000_000
    assert db.charge(acc, 9_000_000, room="r", mode="normal", usd=0.2) == 0
    conn = db.connect()
    rows = conn.execute("SELECT charge_ueur, balance_after_ueur FROM usage ORDER BY id").fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [(4_000_000, 6_000_000), (9_000_000, 0)]


# ----- Gating ---------------------------------------------------------------------
def test_gating_off_by_default_calls_work_without_wallet(client):
    assert client.post("/api/host-call", json={"identity": "u"}).status_code == 200


def test_gating_normal_on_402_without_or_empty_wallet(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "1")
    r = client.post("/api/host-call", json={"identity": "u"})
    assert r.status_code == 402
    assert r.json()["detail"]["topup_url"] == "/aufladen"
    w = _paid_wallet(client)
    acc = db.account_for_token(w["wallet_token"])
    ok = client.post("/api/host-call", json={"identity": "u"}, headers={"x-wallet-token": w["wallet_token"]})
    assert ok.status_code == 200                                   # Positivkontrolle
    assert db.room_wallet(ok.json()["room"]) == (acc, "normal")    # Raum hängt am Konto
    db.charge(acc, 10**9, room="x", mode="normal", usd=1)
    empty = client.post("/api/host-call", json={"identity": "u"}, headers={"x-wallet-token": w["wallet_token"]})
    assert empty.status_code == 402 and empty.json()["detail"]["wallet"] == "empty"


def test_gating_live_independent_of_normal(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_LIVE", "1")
    assert client.post("/api/host-call", json={"identity": "u"}).status_code == 200
    assert client.post("/api/live-room", json={"identity": "u"}).status_code == 402
    w = _paid_wallet(client)
    r = client.post("/api/live-room", json={"identity": "u"}, headers={"x-wallet-token": w["wallet_token"]})
    assert r.status_code == 200
    assert db.room_wallet(r.json()["room"])[1] == "live"


def test_gating_off_wallet_still_binds_and_empty_wallet_runs_free(client):
    w = _paid_wallet(client)
    r = client.post("/api/host-call", json={"identity": "u"}, headers={"x-wallet-token": w["wallet_token"]})
    assert db.room_wallet(r.json()["room"]) is not None
    acc = db.account_for_token(w["wallet_token"])
    db.charge(acc, 10**9, room="x", mode="normal", usd=1)
    r2 = client.post("/api/host-call", json={"identity": "u"}, headers={"x-wallet-token": w["wallet_token"]})
    assert r2.status_code == 200 and db.room_wallet(r2.json()["room"]) is None


def test_invite_join_binds_only_unbound_room(client):
    from agent.tokens import mint_invite

    w1 = _paid_wallet(client, "cs_1")
    w2 = _paid_wallet(client, "cs_2")
    inv = mint_invite("room-a", secret="test-secret-do-not-use")
    body = {"room": "room-a", "identity": "g", "invite": inv}
    client.post("/api/token", json=body, headers={"x-wallet-token": w1["wallet_token"]})
    client.post("/api/token", json=body, headers={"x-wallet-token": w2["wallet_token"]})
    assert db.room_wallet("room-a")[0] == db.account_for_token(w1["wallet_token"])


def test_credits_required_flag_parsing(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "aus")
    assert billing_routes.credits_required("normal") is False
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "1")
    assert billing_routes.credits_required("normal") is True
    assert billing_routes.credits_required("live") is False
