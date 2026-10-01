"""Zweites adversariales Review PR #88 (01.10.2026): je Befund ein Regressionstest
mit Positivkontrolle. Kein Test erreicht LiveKit oder Stripe."""

from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
import agent.worker as w
from agent import budget, freetier
from agent.billing import db
from agent.server import app
from agent.tokens import mint_invite

from .test_billing import _event, _paid_wallet, _post_webhook
from .test_freetier import _run_free
from .test_wallet_worker import _account, _run
from .test_worker import _metric

SECRET = "test-secret-do-not-use"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_do_not_use")
    monkeypatch.setenv("VOICEHOOK_PUBLIC_URL", "https://vh.test")
    monkeypatch.setenv("INVITE_SECRET", SECRET)
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    for k in ("VOICEHOOK_REQUIRE_CREDITS_NORMAL", "VOICEHOOK_REQUIRE_CREDITS_LIVE",
              "VH_FREE_EUR_PER_DAY",
              "VOICEHOOK_LIVE_PUBLIC", "VH_FREE_TICK_SECONDS", "VOICEHOOK_PRICE_FACTOR_NORMAL",
              "VOICEHOOK_PRICE_FACTOR_LIVE", "VOICEHOOK_VAT_RATE", "VOICEHOOK_USD_EUR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_HOST_LIMIT", 10_000)
    srv._HOST_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


# ----- #1 HIGH: Raum-Wallet-Bindung endet (Call-Ende / TTL) -------------------------
def test_binding_states_active_expired_closed():
    acc = _account(1000, "cs_b1")
    t0 = 1_000_000.0
    db.bind_room("rb", acc, "normal", 600, now=t0)
    assert db.room_binding("rb", now=t0 + 599)[2] == "active"          # Positivkontrolle
    assert db.room_wallet("rb", now=t0 + 599) == (acc, "normal")
    assert db.room_binding("rb", now=t0 + 600)[2] == "expired"
    assert db.room_wallet("rb", now=t0 + 600) is None
    db.bind_room("rc", acc, "normal", 600)
    assert db.close_room("rc") is True and db.close_room("rc") is False
    assert db.room_binding("rc")[2] == "closed" and db.room_wallet("rc") is None


def test_call_end_closes_binding_and_rejoin_is_refused(monkeypatch):
    acc = _account(1000, "cs_b2")
    db.bind_room("r1", acc, "normal")
    db.charge(acc, 10_000_000 - 100, room="r1", mode="normal", usd=0)
    ctx, _, _ = _run(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=10.0)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:wallet_empty")
    assert db.room_binding("r1")[2] == "closed"                          # Call-Ende schließt
    _post_webhook(TestClient(app), _event("cs_b2_top", amount_cents=1000, metadata={"vh_account": acc}))
    assert db.balance_ueur(acc) > 0                                      # Konto wieder gedeckt
    ctx2, _, built = _run(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=600.0)])
    assert built == []                                                   # keine Session
    ctx2.shutdown.assert_called_once_with(reason="wallet_binding_closed")
    assert db.balance_ueur(acc) == 10_000_000                            # nichts abgebucht


def test_worker_refuses_expired_binding_and_runs_active_one(monkeypatch):
    acc = _account(1000, "cs_b3")
    db.bind_room("r1", acc, "normal", 3600, now=time.time() - 7200)     # längst abgelaufen
    ctx, _, built = _run(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=60.0)])
    assert built == [] and db.balance_ueur(acc) == 10_000_000
    ctx.shutdown.assert_called_once_with(reason="wallet_binding_closed")
    db.bind_room("r2", acc, "normal", 3600)                              # Positivkontrolle
    ctx, _, built = _run(monkeypatch, live_mode=False, room="r2",
                         metrics=[_metric("STTMetrics", audio_duration=60.0)])
    assert built == [1] and db.balance_ueur(acc) < 10_000_000
    ctx.shutdown.assert_not_called()


def test_invite_join_only_for_active_wallet_rooms(client):
    paid = _paid_wallet(client, "cs_b4")
    r = client.post("/api/host-call", json={"identity": "h", "ttl_seconds": 600},
                    headers={"x-wallet-token": paid["wallet_token"]})
    room = r.json()["room"]
    exp = db.room_binding(room)
    assert exp[2] == "active"
    q = {"room": room, "identity": "op", "invite": "1"}
    assert client.get("/api/token", params=q).status_code == 200        # Positivkontrolle
    inv = mint_invite(room, secret=SECRET)
    assert client.post("/api/token", json={"room": room, "identity": "g", "invite": inv}).status_code == 200
    db.close_room(room)
    assert client.get("/api/token", params=q).status_code == 410        # Slug allein reicht nicht mehr
    assert client.post("/api/token", json={"room": room, "identity": "g", "invite": inv}).status_code == 410
    # Räume ohne Bindung (Operator/Gratis) bleiben wie bisher
    assert client.get("/api/token", params={**q, "room": "plain-room"}).status_code == 200


def test_binding_ttl_follows_requested_token_ttl(client):
    paid = _paid_wallet(client, "cs_b5")
    before = time.time()
    room = client.post("/api/host-call", json={"identity": "h", "ttl_seconds": 600},
                       headers={"x-wallet-token": paid["wallet_token"]}).json()["room"]
    conn = db.connect()
    exp = conn.execute("SELECT expires_at FROM room_wallets WHERE room = ?", (room,)).fetchone()[0]
    conn.close()
    assert before + 600 <= exp <= time.time() + 600


# ----- #2 MEDIUM: Live fail-closed, free_rooms länger als Raumzuordnung -------------
def test_live_worker_refuses_room_without_wallet_and_free_entry(monkeypatch):
    ctx, session = _run_free(monkeypatch, room="ghost", humans=1, wait_s=0.1)
    ctx.shutdown.assert_called_once_with(reason="free_room_unknown")
    session.start.assert_not_called()
    freetier.register_room("known", "live", freetier.identity_keys("anon-xxxxxxxx", "4.4.4.4"))
    ctx, session = _run_free(monkeypatch, room="known", humans=1, wait_s=0.1)   # Positivkontrolle
    ctx.shutdown.assert_not_called()
    session.start.assert_awaited_once()


def test_live_fail_closed_off_when_free_tier_disabled(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0")         # Limit aus = bewusst frei
    ctx, _ = _run_free(monkeypatch, room="ghost2", humans=1, wait_s=0.1)
    ctx.shutdown.assert_not_called()


def test_free_rooms_kept_longer_than_room_assignment():
    t = time.time()
    freetier.register_room("old", "live", ["k"], now=t - 3 * 86400)      # früher nach 2 Tagen weg
    freetier.register_room("new", "live", ["k"], now=t)
    assert freetier.room_keys("old") is not None
    freetier.register_room("older", "live", ["k"], now=t - 8 * 86400)
    freetier.register_room("new2", "live", ["k"], now=t)
    assert freetier.room_keys("older") is None                           # Aufräumen greift weiter
    assert srv._ROOM_AGENT_MAX_TTL < freetier._KEEP_DAYS * 86400


def test_room_agent_assignment_expires(monkeypatch):
    srv._set_room_agent("ra", "voice-ai-live", 60)
    assert srv._agent_for("ra") == "voice-ai-live"                       # Positivkontrolle
    real = time.time
    monkeypatch.setattr(srv.time, "time", lambda: real() + 61)
    assert srv._agent_for("ra") == "voice-ai"


def test_admin_live_room_is_registered_exempt(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", "adminkey-test")
    r = client.post("/api/admin/live-room", json={}, headers={"authorization": "Bearer adminkey-test"})
    room = r.json()["room"]
    assert freetier.room_keys(room) == ("live", [])                      # bekannt, nicht gezählt


# ----- #3 MEDIUM: IPv6 je /64 ----------------------------------------------------------
def test_ipv6_counted_per_64():
    a = freetier.identity_keys(None, "2001:db8:1:2::1")
    assert a == freetier.identity_keys(None, "2001:db8:1:2:ffff:eeee:dddd:9")
    assert a != freetier.identity_keys(None, "2001:db8:1:3::1")         # Positivkontrolle: anderes /64
    assert freetier.identity_keys(None, "::ffff:1.2.3.4") == freetier.identity_keys(None, "1.2.3.4")
    assert freetier.ip_bucket("unknown") == "unknown"


def test_ipv6_rotation_in_same_64_hits_limit(client):
    freetier.add_ueur(freetier.identity_keys(None, "2001:db8:aa:bb::1"), 1_000_000)
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "2001:db8:aa:bb:1234::77"})
    assert r.status_code == 402
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "2001:db8:aa:cc::1"})
    assert r.status_code == 200


# ----- #4 LOW: X-Forwarded-For --------------------------------------------------------
def test_client_ip_uses_last_nonempty_forwarded_element():
    from types import SimpleNamespace

    def req(xff):
        return SimpleNamespace(headers={"x-forwarded-for": xff}, client=SimpleNamespace(host="127.0.0.1"))

    assert srv._client_ip(req("6.6.6.6, 7.7.7.7")) == "7.7.7.7"
    assert srv._client_ip(req("7.7.7.7, ")) == "7.7.7.7"
    assert srv._client_ip(req("")) == "127.0.0.1"


def test_caddyfile_documents_trusted_proxies():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[3] / "infra/caddy/Caddyfile.tmpl").read_text()
    assert "trusted_proxies" in text


# ----- #6 LOW: Fehlbetrag aus Erstattung/Rückbuchung bei nächster Gutschrift ---------
def test_refund_shortfall_is_offset_on_next_topup(client):
    paid = _paid_wallet(client, "cs_d1", amount_cents=1000)
    acc = db.account_for_token(paid["wallet_token"])
    db.charge(acc, 10_000_000, room="r", mode="normal", usd=1)          # alles verbraucht
    res = db.reverse_payment("pi_cs_d1", "refund:x", "refund", 1000)
    assert res["shortfall_ueur"] == 10_000_000
    assert db.account(acc)["debt_ueur"] == 10_000_000
    _post_webhook(client, _event("cs_d2", amount_cents=2000, metadata={"vh_account": acc}))
    assert db.balance_ueur(acc) == 10_000_000                            # 20 - 10 Schuld
    assert db.account(acc)["debt_ueur"] == 0
    _post_webhook(client, _event("cs_d3", amount_cents=2000, metadata={"vh_account": acc}))
    assert db.balance_ueur(acc) == 30_000_000                            # Positivkontrolle: Schuld nur einmal


def test_debt_larger_than_topup_carries_over(client):
    paid = _paid_wallet(client, "cs_d4", amount_cents=5000)
    acc = db.account_for_token(paid["wallet_token"])
    db.charge(acc, 50_000_000, room="r", mode="normal", usd=1)
    db.reverse_payment("pi_cs_d4", "dispute:y", "dispute", 5000)
    _post_webhook(client, _event("cs_d5", amount_cents=2000, metadata={"vh_account": acc}))
    assert db.balance_ueur(acc) == 0 and db.account(acc)["debt_ueur"] == 30_000_000


def test_old_billing_file_gets_new_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "old"))
    (tmp_path / "old").mkdir()
    c = sqlite3.connect(tmp_path / "old" / "billing.sqlite")
    c.executescript(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, email TEXT NOT NULL, balance_ueur INTEGER NOT NULL"
        " DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        "CREATE TABLE room_wallets (room TEXT PRIMARY KEY, account_id TEXT NOT NULL, mode TEXT NOT NULL,"
        " created_at TEXT NOT NULL);"
        "INSERT INTO accounts VALUES ('acc_old','a@b.c',5,'t','t');"
        "INSERT INTO room_wallets VALUES ('legacy','acc_old','normal','t');"
    )
    c.commit()
    c.close()
    assert db.account("acc_old")["debt_ueur"] == 0
    assert db.room_binding("legacy")[2] == "expired"                     # Altbestand: fail-closed


# ----- #7 INFO: Recovery rotiert, Lesen ohne Schreibsperre, Budget nur Gratis --------
def test_recovery_code_rotates(client):
    paid = _paid_wallet(client, "cs_r1")
    old = paid["recovery_url"].split("#r=", 1)[1]
    r = client.post("/api/wallet/recover", json={"code": old})
    assert r.status_code == 200                                          # Positivkontrolle
    new = r.json()["recovery_url"].split("#r=", 1)[1]
    assert new != old
    assert client.post("/api/wallet/recover", json={"code": old}).status_code == 404
    r2 = client.post("/api/wallet/recover", json={"code": new})
    assert r2.status_code == 200
    assert db.account_for_token(r2.json()["wallet_token"]) == db.account_for_token(paid["wallet_token"])


def test_aufladen_page_shows_rotated_recovery_link():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[3] / "web/aufladen.html").read_text()
    assert "showDone(d, d.recovery_url, T.restored)" in html


def test_account_for_token_does_not_wait_for_write_lock():
    acc = _account(1000, "cs_l1")
    tok = db.issue_token(acc)
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        t = time.monotonic()
        assert db.account_for_token(tok) == acc
        assert time.monotonic() - t < 1.0                                # früher: 5 s Busy-Timeout
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert db.account_for_token(tok) == acc                              # Positivkontrolle: last_used gesetzt
    conn = db.connect()
    assert conn.execute("SELECT last_used_at FROM tokens WHERE kind='wallet'").fetchone()[0]
    conn.close()


def test_paying_customer_not_blocked_by_month_budget(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    budget.add_usd(5.0)
    assert budget.exhausted()
    assert client.post("/api/live-room", json={"identity": "u"},
                       headers={"x-forwarded-for": "8.8.8.8"}).status_code == 402   # Gratis: gesperrt
    assert client.get("/api/live/status").json() == {"available": False}
    paid = _paid_wallet(client, "cs_bud")
    h = {"x-forwarded-for": "8.8.8.8", "x-wallet-token": paid["wallet_token"]}
    r = client.post("/api/live-room", json={"identity": "u"}, headers=h)
    assert r.status_code == 200
    assert db.room_wallet(r.json()["room"])[1] == "live"
    assert client.get("/api/live/status", headers=h).json() == {"available": True}


def test_live_worker_runs_wallet_room_despite_budget(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    budget.add_usd(5.0)
    acc = _account(1000, "cs_bw")
    db.bind_room("r1", acc, "live")
    ctx, _, built = _run(monkeypatch, live_mode=True, metrics=[])
    assert built == [1]
    ctx.shutdown.assert_not_called()
    freetier.register_room("r2", "live", [], exempt=True)                # Gratis/Demo: weiter gesperrt
    ctx, _, built = _run(monkeypatch, live_mode=True, room="r2", metrics=[])
    assert built == []
    ctx.shutdown.assert_called_once_with(reason="live_budget_exhausted")
    assert w.LIVE_BUDGET_ANNOUNCEMENT
