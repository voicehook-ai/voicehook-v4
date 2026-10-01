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
              "VOICEHOOK_TOPUP_MIN_EUR", "VOICEHOOK_TOPUP_MAX_EUR",
              "VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "VOICEHOOK_FREE_MIN_PER_DAY_NORMAL"):
        monkeypatch.delenv(k, raising=False)
    # Kein Test darf LiveKit erreichen
    import agent.server as srv
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    srv._HOST_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _event(session_id="cs_test_1", email="Kunde@Example.com", amount_cents=2000, *,
           etype="checkout.session.completed", paid=True, currency="eur", metadata=None,
           payment_intent=None):
    return {
        "id": "evt_" + session_id, "type": etype,
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "amount_total": amount_cents,
            "currency": currency, "payment_status": "paid" if paid else "unpaid",
            "customer_details": {"email": email}, "metadata": metadata or {},
            "payment_intent": payment_intent or "pi_" + session_id,
        }},
    }


def _post_webhook(client, event, *, secret=WH_SECRET, ts=None, sig=None):
    body = json.dumps(event).encode()
    header = sig or stripe_api.sign(body, secret, int(ts or time.time()))
    return client.post("/api/stripe/webhook", content=body,
                       headers={"stripe-signature": header, "content-type": "application/json"})


def _paid_wallet(client, session_id="cs_test_w", amount_cents=1000, email="Kunde@Example.com"):
    assert _post_webhook(client, _event(session_id, email=email, amount_cents=amount_cents)).status_code == 200
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
    assert "email" not in w                                           # API gibt nie eine E-Mail aus


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
    assert _post_webhook(client, _event("cs_c", etype="invoice.paid")).json()["ignored"] == "invoice.paid"
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


def test_invite_join_never_binds_room_to_guest_wallet(client):
    """Review 01.10. (MEDIUM): nur der Ersteller zahlt. Ein Gast mit Wallet, der per
    Einladung in einen (Gratis-)Raum kommt, zahlt nie für den fremden Call."""
    from agent.tokens import mint_invite

    host = client.post("/api/host-call", json={"identity": "host"})       # Raum ohne Wallet
    room = host.json()["room"]
    guest = _paid_wallet(client, "cs_guest")
    inv = mint_invite(room, secret="test-secret-do-not-use")
    r = client.post("/api/token", json={"room": room, "identity": "g", "invite": inv},
                    headers={"x-wallet-token": guest["wallet_token"]})
    assert r.status_code == 200                                            # Beitritt klappt
    assert db.room_wallet(room) is None                                    # aber keine Bindung
    acc = db.account_for_token(guest["wallet_token"])
    assert db.balance_ueur(acc) == 10_000_000
    # Positivkontrolle: legt derselbe Gast selbst einen Raum an, zahlt er dafür.
    own = client.post("/api/host-call", json={"identity": "g"},
                      headers={"x-wallet-token": guest["wallet_token"]})
    assert db.room_wallet(own.json()["room"]) == (acc, "normal")


def test_token_endpoint_has_no_wallet_binding_path():
    assert not hasattr(billing_routes, "bind_room_if_paid")


def test_credits_required_flag_parsing(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "aus")
    assert billing_routes.credits_required("normal") is False
    monkeypatch.setenv("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "1")
    assert billing_routes.credits_required("normal") is True
    assert billing_routes.credits_required("live") is False


# ----- Review 01.10. CRITICAL: Konto-Übernahme per E-Mail ------------------------
def test_foreign_email_at_checkout_never_reaches_victim_account(client):
    """Opfer lädt 200 auf; Angreifer zahlt 10 und tippt die Opfer-E-Mail ein.
    Angreifer sieht nur seine 10 und keine fremde E-Mail; Opfer behält 200."""
    victim = _paid_wallet(client, "cs_victim", amount_cents=20_000, email="victim@x.de")
    assert _post_webhook(client, _event("cs_attacker", email="Victim@X.de", amount_cents=1000)).status_code == 200
    stolen = client.post("/api/wallet/claim", json={"session_id": "cs_attacker"})
    assert stolen.status_code == 200
    body = stolen.json()
    assert body["balance_eur"] == 10.0                                  # nur das eigene Geld
    assert "victim" not in json.dumps(body).lower()                     # keine fremde E-Mail
    att = client.get("/api/wallet", headers={"x-wallet-token": body["wallet_token"]}).json()
    assert att["balance_eur"] == 10.0 and "email" not in att
    vic = client.get("/api/wallet", headers={"x-wallet-token": victim["wallet_token"]}).json()
    assert vic["balance_eur"] == 200.0                                  # Positivkontrolle Opfer
    assert db.account_for_token(body["wallet_token"]) != db.account_for_token(victim["wallet_token"])
    # Recovery-Link des Angreifers führt ebenfalls nur zu seinem Konto
    code = body["recovery_url"].split("#r=", 1)[1]
    rec = client.post("/api/wallet/recover", json={"code": code}).json()
    assert rec["balance_eur"] == 10.0


def test_same_email_twice_without_wallet_creates_two_accounts():
    assert db.record_stripe_session("cs_e1", "same@x.de", 1000) is True
    assert db.record_stripe_session("cs_e2", "same@x.de", 2000) is True
    a1, a2 = db.claim_session("cs_e1")[1], db.claim_session("cs_e2")[1]
    assert a1 != a2
    assert (db.balance_ueur(a1), db.balance_ueur(a2)) == (10_000_000, 20_000_000)


def test_forged_unknown_vh_account_creates_new_account():
    db.record_stripe_session("cs_v", "v@x.de", 5000)
    victim = db.claim_session("cs_v")[1]
    db.record_stripe_session("cs_f", "v@x.de", 1000, account_id="acc_does_not_exist")
    other = db.claim_session("cs_f")[1]
    assert other != victim and db.balance_ueur(victim) == 50_000_000


def test_old_schema_with_unique_email_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "old"))
    path = db.db_path()
    path.parent.mkdir(parents=True)
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,"
        " balance_ueur INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE stripe_sessions (session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
        " amount_cents INTEGER NOT NULL, processed_at TEXT NOT NULL, claimed_at TEXT);"
        "INSERT INTO accounts VALUES ('acc_old', 'a@b.c', 5, 't', 't');"
    )
    c.close()
    assert db.record_stripe_session("cs_m1", "a@b.c", 1000, payment_intent="pi_m1") is True
    assert db.claim_session("cs_m1")[1] != "acc_old"
    assert db.balance_ueur("acc_old") == 5


# ----- Review 01.10. LOW: Erstattung / Rückbuchung --------------------------------
def _charge_event(etype, *, pi, oid, amount_refunded=0, amount=0):
    obj = {"id": oid, "payment_intent": pi}
    if etype == "charge.refunded":
        obj.update(object="charge", amount_refunded=amount_refunded)
    else:
        obj.update(object="dispute", amount=amount)
    return {"id": "evt_" + oid + str(amount_refunded), "type": etype, "data": {"object": obj}}


def test_refund_debits_idempotent_and_partial_cumulative(client):
    w = _paid_wallet(client, "cs_r", amount_cents=2000)          # 20 EUR, pi_cs_r
    hdr = {"x-wallet-token": w["wallet_token"]}
    r = _post_webhook(client, _charge_event("charge.refunded", pi="pi_cs_r", oid="ch_1", amount_refunded=500))
    assert r.json() == {"received": True, "reversal": "reversed"}
    assert client.get("/api/wallet", headers=hdr).json()["balance_eur"] == 15.0
    again = _post_webhook(client, _charge_event("charge.refunded", pi="pi_cs_r", oid="ch_1", amount_refunded=500))
    assert again.json()["reversal"] == "duplicate"                # Wiederzustellung
    assert client.get("/api/wallet", headers=hdr).json()["balance_eur"] == 15.0
    # zweite Teilerstattung: Stripe meldet kumuliert 1200 -> nur 7 EUR Zuwachs
    _post_webhook(client, _charge_event("charge.refunded", pi="pi_cs_r", oid="ch_1", amount_refunded=1200))
    assert client.get("/api/wallet", headers=hdr).json()["balance_eur"] == 8.0


def test_dispute_debits_never_below_zero_and_notes_shortfall(client):
    w = _paid_wallet(client, "cs_d", amount_cents=1000)
    acc = db.account_for_token(w["wallet_token"])
    db.charge(acc, 7_000_000, room="r", mode="normal", usd=1)     # 3 EUR übrig, 7 verbraucht
    r = _post_webhook(client, _charge_event("charge.dispute.created", pi="pi_cs_d", oid="dp_1", amount=1000))
    assert r.json()["reversal"] == "reversed"
    assert db.balance_ueur(acc) == 0                              # nie unter 0
    conn = db.connect()
    row = conn.execute("SELECT debited_ueur, shortfall_ueur FROM reversals WHERE key='dispute:dp_1'").fetchone()
    conn.close()
    assert tuple(row) == (3_000_000, 7_000_000)                   # Fehlbetrag vermerkt
    assert _post_webhook(client, _charge_event("charge.dispute.created", pi="pi_cs_d", oid="dp_1",
                                               amount=1000)).json()["reversal"] == "duplicate"


def test_refund_plus_dispute_capped_at_paid_amount(client):
    w = _paid_wallet(client, "cs_cap", amount_cents=1000)
    acc = db.account_for_token(w["wallet_token"])
    db.record_stripe_session("cs_other", "", 5000, account_id=acc)   # weiteres Guthaben, anderes pi
    _post_webhook(client, _charge_event("charge.refunded", pi="pi_cs_cap", oid="ch_c", amount_refunded=1000))
    _post_webhook(client, _charge_event("charge.dispute.created", pi="pi_cs_cap", oid="dp_c", amount=1000))
    assert db.balance_ueur(acc) == 50_000_000                     # 10 EUR nur einmal zurück


def test_refund_unknown_payment_and_bad_signature_change_nothing(client):
    w = _paid_wallet(client, "cs_u", amount_cents=1000)
    hdr = {"x-wallet-token": w["wallet_token"]}
    ev = _charge_event("charge.refunded", pi="pi_fremd", oid="ch_x", amount_refunded=1000)
    assert _post_webhook(client, ev).json()["reversal"] == "unknown_payment"
    ev2 = _charge_event("charge.refunded", pi="pi_cs_u", oid="ch_y", amount_refunded=1000)
    assert _post_webhook(client, ev2, secret="whsec_wrong").status_code == 400
    assert client.get("/api/wallet", headers=hdr).json()["balance_eur"] == 10.0   # Positivkontrolle davor
    assert _post_webhook(client, ev2).json()["reversal"] == "reversed"
    assert client.get("/api/wallet", headers=hdr).json()["balance_eur"] == 0.0
