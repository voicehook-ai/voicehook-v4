"""Gratis-Kontingent ohne Login (Oliver 01.10.): 1 EUR Kundenpreis-Verbrauch pro UTC-Tag
je Merkmal (Anon-ID aus X-Anon-Id, Client-IP), Normal und Live gemeinsam. Gebucht wird
nur aus echten Kostenereignissen. Bezahlte Räume ausgenommen."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import agent.worker as w
from agent import freetier
from agent.billing import db
from agent.server import app

from .test_billing import _paid_wallet
from .test_worker import _Emitter, _metric

ANON_A = "anon-aaaaaaaa-1111"
ANON_B = "anon-bbbbbbbb-2222"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_do_not_use")
    monkeypatch.setenv("INVITE_SECRET", "test-secret-do-not-use")
    monkeypatch.setenv("LIVEKIT_API_KEY", "API_TEST")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret_test_value")
    monkeypatch.setenv("LIVEKIT_URL", "wss://rtc.test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    for k in ("VH_FREE_EUR_PER_DAY",
              "VOICEHOOK_REQUIRE_CREDITS_NORMAL", "VOICEHOOK_REQUIRE_CREDITS_LIVE",
              "VOICEHOOK_LIVE_PUBLIC", "VH_FREE_TICK_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    import agent.server as srv
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_HOST_LIMIT", 10_000)  # Ratenlimit ist hier nicht Thema
    srv._HOST_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _live(client, anon=None, ip="1.1.1.1", wallet=None):
    h = {"x-forwarded-for": ip}
    if anon:
        h["x-anon-id"] = anon
    if wallet:
        h["x-wallet-token"] = wallet
    return client.post("/api/live-room", json={"identity": "u"}, headers=h)


def _use(anon=None, ip="1.1.1.1", eur=1.0):
    freetier.add_ueur(freetier.identity_keys(anon, ip), round(eur * 1_000_000))


def _expected_ueur(usd, mode):
    """Kundenpreis von Hand: USD x Kurs x Faktor (Normal 3, Live 1,5) x 1,19 MwSt."""
    f = 3.0 if mode == "normal" else 1.5
    return max(1, round(usd * 0.8807 * f * 1.19 * 1_000_000))


# ----- Konfiguration ------------------------------------------------------------
def test_default_one_euro_per_day():
    assert freetier.limit_eur() == 1.0 and freetier.limit_ueur() == 1_000_000
    assert freetier.enabled("normal") and freetier.enabled("live")


def test_env_zero_is_off(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0")
    assert not freetier.enabled() and freetier.limit_ueur() == 0


@pytest.mark.parametrize("bad", ["kaputt", "-1", "inf", "nan", "1,5"])
def test_env_broken_means_zero_free_but_still_checked(monkeypatch, client, bad):
    """Kaputter Wert -> 0 EUR Gratis, Prüfung bleibt an: nie unbegrenzt."""
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", bad)
    assert freetier.limit_ueur() == 0 and freetier.enabled()
    r = _live(client, ANON_A)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"


def test_env_custom_value(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "2.5")
    assert freetier.limit_ueur() == 2_500_000


def test_old_minute_envs_are_ignored(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", "0")
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "0")
    assert freetier.enabled() and freetier.limit_ueur() == 1_000_000


def test_identity_keys_hashed_and_invalid_anon_ignored():
    keys = freetier.identity_keys(ANON_A, "9.9.9.9")
    assert len(keys) == 2 and all("9.9.9.9" not in k and ANON_A not in k for k in keys)
    assert freetier.identity_keys("x", "9.9.9.9") == freetier.identity_keys(None, "9.9.9.9")  # zu kurz
    assert len(freetier.identity_keys("bad id with spaces!", "9.9.9.9")) == 1


def test_schema_migration_idempotent_and_old_seconds_ignored():
    conn = freetier.connect()
    conn.execute("INSERT INTO free_usage (day, mode, key, seconds) VALUES (?, 'live', 'k1', 99999)",
                 (freetier.day_key(),))
    conn.close()
    freetier._INITIALIZED.clear()                                 # zweiter Start: Schema erneut
    freetier.connect().close()
    assert freetier.remaining_ueur(["k1"]) == 1_000_000           # alte Sekunden zählen nicht


def test_consume_takes_until_zero_and_returns_rest():
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    assert freetier.consume_ueur(keys, 400_000) == (400_000, 600_000)
    assert freetier.consume_ueur(keys, 700_000) == (600_000, 0)   # Grenze: Rest bis 0
    assert freetier.consume_ueur(keys, 5) == (0, 0)
    assert freetier.used_ueur(keys) == 1_000_000


# ----- HTTP: 402 free_limit -----------------------------------------------------
def test_live_room_under_limit_ok_and_registers_room(client):
    _use(ANON_A, eur=0.99)
    r = _live(client, ANON_A)
    assert r.status_code == 200                                   # Positivkontrolle
    mode, keys = freetier.room_keys(r.json()["room"])
    assert mode == "live" and set(keys) == set(freetier.identity_keys(ANON_A, "1.1.1.1"))


def test_live_room_at_limit_is_402_free_limit(client):
    _use(ANON_A, eur=1.0)
    r = _live(client, ANON_A)
    assert r.status_code == 402
    assert r.json()["detail"] == {"error": "free_limit", "topup_url": "/aufladen", "free_eur_per_day": 1.0}


def test_limit_hits_when_either_anon_or_ip_reached(client):
    _use(ANON_A, ip="1.1.1.1")
    assert _live(client, ANON_A, ip="2.2.2.2").status_code == 402   # gleiche Anon-ID, neue IP
    assert _live(client, ANON_B, ip="1.1.1.1").status_code == 402   # neue Anon-ID, gleiche IP
    assert _live(client, None, ip="1.1.1.1").status_code == 402     # ohne Header: nur IP
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 200   # Positivkontrolle: beides frisch


def test_ip_from_last_forwarded_for_element(client):
    """Review 01.10. #4: das letzte Element hat Caddy gesetzt, davor kann der Client
    beliebiges eintragen. Ein vorangestellter Fake-Wert umgeht die Sperre nicht."""
    _use(None, ip="5.5.5.5")
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "9.9.9.9, 5.5.5.5"})
    assert r.status_code == 402
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "5.5.5.5, 6.6.6.6"})
    assert r.status_code == 200


def test_new_utc_day_resets(client):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    yesterday = time.time() - 86400
    freetier.add_ueur(keys, 1_000_000, now=yesterday)
    assert freetier.remaining_ueur(keys, now=yesterday) == 0        # gestern: leer
    assert freetier.remaining_ueur(keys) == 1_000_000               # heute: wieder 1 EUR
    assert _live(client, ANON_A).status_code == 200


def test_paid_wallet_is_exempt_and_room_not_counted(client):
    _use(ANON_A)
    wl = _paid_wallet(client, "cs_free")
    r = _live(client, ANON_A, wallet=wl["wallet_token"])
    assert r.status_code == 200
    assert freetier.room_keys(r.json()["room"]) is None
    assert db.room_wallet(r.json()["room"])[1] == "live"


def test_empty_wallet_with_gating_off_falls_back_to_free_limit(client):
    _use(ANON_A)
    wl = _paid_wallet(client, "cs_empty")
    db.charge(db.account_for_token(wl["wallet_token"]), 10**9, room="x", mode="live", usd=1)
    assert _live(client, ANON_A, wallet=wl["wallet_token"]).status_code == 402


def test_normal_and_live_share_one_pot(client):
    _use(ANON_A, eur=1.0)                                           # z. B. im Normalmodus verbraucht
    h = {"x-anon-id": ANON_A, "x-forwarded-for": "1.1.1.1"}
    assert client.post("/api/host-call", json={"identity": "u"}, headers=h).status_code == 402
    assert _live(client, ANON_A).status_code == 402                 # Live ist mit leer
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 200   # Positivkontrolle


def test_off_via_env_rooms_not_counted(client, monkeypatch):
    _use(ANON_A)
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0")
    h = {"x-anon-id": ANON_A, "x-forwarded-for": "1.1.1.1"}
    ok = client.post("/api/host-call", json={"identity": "u"}, headers=h)
    assert ok.status_code == 200 and freetier.room_keys(ok.json()["room"]) is None


# ----- Worker: bucht nur echte Kosten, beendet am leeren Topf ---------------------
def _run_free(monkeypatch, *, room, humans, wait_s, live_mode=True, metrics=(), gap_s=0.02,
              clock_jump_s=0.0):
    """Treibt entrypoint mit Fake-Raum/Session. `metrics` werden nacheinander als
    metrics_collected gesendet; `clock_jump_s` lässt time.monotonic (auch die Uhr der
    Event-Loop) in 10 Schritten so weit vorlaufen (simulierte Calldauer)."""
    if live_mode:
        monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
    else:
        monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
        monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
    monkeypatch.setenv("VH_FREE_TICK_SECONDS", "0.02")
    session = _Emitter()
    session.start = AsyncMock()
    session.aclose = AsyncMock()
    session.say = MagicMock(return_value=SimpleNamespace(wait_for_playout=AsyncMock()))
    session.generate_reply = MagicMock(return_value=SimpleNamespace(wait_for_playout=AsyncMock()))
    monkeypatch.setattr(w, "build_session", lambda: session)
    r = _Emitter()
    r.name = room
    r.remote_participants = {f"h{i}": SimpleNamespace(identity=f"user-{i}", kind=None) for i in range(humans)}
    r.local_participant = SimpleNamespace(identity="voice-ai", publish_data=AsyncMock())
    ctx = SimpleNamespace(connect=AsyncMock(), room=r, job=SimpleNamespace(id="j1"),
                          shutdown=MagicMock(), delete_room=AsyncMock())
    offset = [0.0]
    if clock_jump_s:
        real = time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])

    async def _go():
        await w.entrypoint(ctx)
        await asyncio.sleep(0.05)                                   # Startprüfung des Topfs
        for m in metrics:
            session.emit("metrics_collected", SimpleNamespace(metrics=m))
            await asyncio.sleep(gap_s)
        for _ in range(10 if clock_jump_s else 0):
            offset[0] += clock_jump_s / 10
            await asyncio.sleep(0.03)
        await asyncio.sleep(wait_s)

    asyncio.run(_go())
    return ctx, session


def _stt(seconds):
    return _metric("STTMetrics", audio_duration=seconds)


def _rt(inp, out):
    return _metric("RealtimeModelMetrics", input_tokens=inp, output_tokens=out,
                   input_token_details=None, output_token_details=None)


def test_worker_books_customer_price_normal_factor_3(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-n1", "normal", keys)
    ctx, _ = _run_free(monkeypatch, room="free-n1", humans=1, wait_s=0.05, live_mode=False,
                       metrics=[_stt(60.0), _stt(30.0)])
    ctx.shutdown.assert_not_called()
    usd = 0.0077 / 60
    want = _expected_ueur(60 * usd, "normal") + _expected_ueur(30 * usd, "normal")
    assert freetier.used_ueur(keys) == want                         # je Ereignis, wie das Wallet
    assert want == 36_315                                           # 90 s STT = 0,036315 EUR brutto


def test_worker_books_customer_price_live_factor_1_5(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-l1", "live", keys)
    ctx, _ = _run_free(monkeypatch, room="free-l1", humans=1, wait_s=0.05, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_not_called()
    usd = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert freetier.used_ueur(keys) == _expected_ueur(usd, "live") == 14_148


def test_worker_normal_and_live_share_pot(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("share-n", "normal", keys)
    _run_free(monkeypatch, room="share-n", humans=1, wait_s=0.05, live_mode=False, metrics=[_stt(60.0)])
    freetier.register_room("share-l", "live", keys)
    _run_free(monkeypatch, room="share-l", humans=1, wait_s=0.05, metrics=[_rt(1000, 500)])
    assert freetier.used_ueur(keys) == _expected_ueur(60 * 0.0077 / 60, "normal") + 14_148


def test_worker_ends_free_call_at_limit_with_announcement(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-1", "live", keys)
    ctx, session = _run_free(monkeypatch, room="free-1", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    said = [c.kwargs["instructions"] for c in session.generate_reply.call_args_list]
    assert sum(w.FREE_LIMIT_ANNOUNCEMENT in t for t in said) == 1
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    ctx.delete_room.assert_awaited_once_with("free-1")
    assert freetier.used_ueur(keys) == 10_000                       # Topf genau bis 0
    assert len(w.FREE_LIMIT_ANNOUNCEMENT) <= 60 and "Guthaben" in w.FREE_LIMIT_ANNOUNCEMENT
    assert "—" not in w.FREE_LIMIT_ANNOUNCEMENT and "–" not in w.FREE_LIMIT_ANNOUNCEMENT


def test_worker_normal_mode_announces_via_say(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    freetier.register_room("free-n", "normal", freetier.identity_keys(ANON_A, "1.1.1.1"))
    ctx, session = _run_free(monkeypatch, room="free-n", humans=1, wait_s=0.2, live_mode=False,
                             metrics=[_stt(600.0)])
    session.say.assert_any_call(w.FREE_LIMIT_ANNOUNCEMENT, allow_interruptions=False)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_under_limit_keeps_running(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-2", "live", keys)
    ctx, session = _run_free(monkeypatch, room="free-2", humans=1, wait_s=0.1, metrics=[_rt(1000, 500)] * 3)
    ctx.shutdown.assert_not_called()                              # Positivkontrolle
    assert freetier.used_ueur(keys) == 3 * 14_148


def test_worker_pot_already_empty_at_start_ends_without_cost(monkeypatch):
    _use(ANON_B, ip="1.1.1.1")                                     # anderer Raum derselben IP: voll
    freetier.register_room("free-4", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))
    ctx, _ = _run_free(monkeypatch, room="free-4", humans=1, wait_s=0.2)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_overflow_goes_to_wallet_and_call_continues(monkeypatch):
    """Grenzüberschreitung: Rest aus dem Topf bis 0, Überhang ans Wallet, kein Ende."""
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    db.record_stripe_session("cs_wk", "", 1000)
    acc = db.claim_session("cs_wk")[1]
    db.bind_room("paid-1", acc, "live")
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("paid-1", "live", keys)
    ctx, session = _run_free(monkeypatch, room="paid-1", humans=1, wait_s=0.1,
                             metrics=[_rt(1000, 500), _rt(1000, 500)])
    ctx.shutdown.assert_not_called()
    assert freetier.used_ueur(keys) == 10_000                       # Topf genau leer
    # 1. Ereignis: 14 148 - 10 000 Überhang; 2. Ereignis voll vom Wallet
    assert db.balance_ueur(acc) == 10_000_000 - (14_148 - 10_000) - 14_148
    said = [c.kwargs.get("instructions", "") for c in session.generate_reply.call_args_list]
    assert not any(w.FREE_LIMIT_ANNOUNCEMENT in t for t in said)


def test_worker_paid_room_empty_wallet_ends_at_free_limit(monkeypatch):
    """Wallet leer, aber Gratis-Rest -> Raum startet (nicht wallet_empty), läuft bis
    der Topf leer ist und endet dann mit free_limit."""
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    db.record_stripe_session("cs_wk2", "", 1000)
    acc = db.claim_session("cs_wk2")[1]
    db.bind_room("paid-2", acc, "live")
    db.charge(acc, 10**9, room="x", mode="live", usd=0)
    freetier.register_room("paid-2", "live", freetier.identity_keys(ANON_B, "4.4.4.4"))
    ctx, _ = _run_free(monkeypatch, room="paid-2", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_book_error_is_fail_closed(monkeypatch):
    freetier.register_room("free-err", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(freetier, "consume_ueur", _boom)
    ctx, _ = _run_free(monkeypatch, room="free-err", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_admin_room_exempt_is_not_limited(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    freetier.register_room("admin-room", "live", [], exempt=True)
    ctx, _ = _run_free(monkeypatch, room="admin-room", humans=1, wait_s=0.1, metrics=[_rt(1000, 500)] * 3)
    ctx.shutdown.assert_not_called()
