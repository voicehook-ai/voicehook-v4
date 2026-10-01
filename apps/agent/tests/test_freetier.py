"""Gratis-Kontingent ohne Login (Oliver 01.10.): max N Gesprächsminuten pro UTC-Tag
je Merkmal (Anon-ID aus X-Anon-Id, Client-IP). Bezahlte Räume ausgenommen."""

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
from .test_worker import _Emitter

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
    for k in ("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "VOICEHOOK_FREE_MIN_PER_DAY_NORMAL",
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


def _use(anon=None, ip="1.1.1.1", minutes=20.0, mode="live"):
    freetier.add_seconds(freetier.identity_keys(anon, ip), mode, minutes * 60)


# ----- Konfiguration ------------------------------------------------------------
def test_defaults_live_20_normal_off(monkeypatch):
    assert freetier.limit_minutes("live") == 20
    assert freetier.limit_minutes("normal") == 0 and not freetier.enabled("normal")
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "kaputt")
    assert freetier.limit_minutes("live") == 20                   # nie unbegrenzt durch Tippfehler
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "0")
    assert not freetier.enabled("live")


def test_identity_keys_hashed_and_invalid_anon_ignored():
    keys = freetier.identity_keys(ANON_A, "9.9.9.9")
    assert len(keys) == 2 and all("9.9.9.9" not in k and ANON_A not in k for k in keys)
    assert freetier.identity_keys("x", "9.9.9.9") == freetier.identity_keys(None, "9.9.9.9")  # zu kurz
    assert len(freetier.identity_keys("bad id with spaces!", "9.9.9.9")) == 1


# ----- HTTP: 402 free_limit -----------------------------------------------------
def test_live_room_under_limit_ok_and_registers_room(client):
    _use(ANON_A, minutes=19.9)
    r = _live(client, ANON_A)
    assert r.status_code == 200                                   # Positivkontrolle
    mode, keys = freetier.room_keys(r.json()["room"])
    assert mode == "live" and set(keys) == set(freetier.identity_keys(ANON_A, "1.1.1.1"))


def test_live_room_at_limit_is_402_free_limit(client):
    _use(ANON_A, minutes=20)
    r = _live(client, ANON_A)
    assert r.status_code == 402
    assert r.json()["detail"] == {"error": "free_limit", "topup_url": "/aufladen", "free_min_per_day": 20.0}


def test_limit_hits_when_either_anon_or_ip_reached(client):
    _use(ANON_A, ip="1.1.1.1", minutes=20)
    assert _live(client, ANON_A, ip="2.2.2.2").status_code == 402   # gleiche Anon-ID, neue IP
    assert _live(client, ANON_B, ip="1.1.1.1").status_code == 402   # neue Anon-ID, gleiche IP
    assert _live(client, None, ip="1.1.1.1").status_code == 402     # ohne Header: nur IP
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 200   # Positivkontrolle: beides frisch


def test_ip_from_last_forwarded_for_element(client):
    """Review 01.10. #4: das letzte Element hat Caddy gesetzt, davor kann der Client
    beliebiges eintragen. Ein vorangestellter Fake-Wert umgeht die Sperre nicht."""
    _use(None, ip="5.5.5.5", minutes=20)
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "9.9.9.9, 5.5.5.5"})
    assert r.status_code == 402
    # Positivkontrolle: eine andere echte (letzte) IP ist frei
    r = client.post("/api/live-room", json={"identity": "u"},
                    headers={"x-forwarded-for": "5.5.5.5, 6.6.6.6"})
    assert r.status_code == 200


def test_new_utc_day_resets(client):
    yesterday = time.time() - 86400
    freetier.add_seconds(freetier.identity_keys(ANON_A, "1.1.1.1"), "live", 3600, now=yesterday)
    assert freetier.used_seconds(freetier.identity_keys(ANON_A, "1.1.1.1"), "live", now=yesterday) == 3600
    assert _live(client, ANON_A).status_code == 200


def test_paid_wallet_is_exempt_and_room_not_counted(client):
    _use(ANON_A, minutes=20)
    wl = _paid_wallet(client, "cs_free")
    r = _live(client, ANON_A, wallet=wl["wallet_token"])
    assert r.status_code == 200
    assert freetier.room_keys(r.json()["room"]) is None
    assert db.room_wallet(r.json()["room"])[1] == "live"


def test_empty_wallet_with_gating_off_falls_back_to_free_limit(client):
    _use(ANON_A, minutes=20)
    wl = _paid_wallet(client, "cs_empty")
    db.charge(db.account_for_token(wl["wallet_token"]), 10**9, room="x", mode="live", usd=1)
    assert _live(client, ANON_A, wallet=wl["wallet_token"]).status_code == 402


def test_normal_off_by_default_and_on_via_env(client, monkeypatch):
    _use(ANON_A, minutes=60, mode="normal")
    h = {"x-anon-id": ANON_A, "x-forwarded-for": "1.1.1.1"}
    ok = client.post("/api/host-call", json={"identity": "u"}, headers=h)
    assert ok.status_code == 200 and freetier.room_keys(ok.json()["room"]) is None
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", "20")
    r = client.post("/api/host-call", json={"identity": "u"}, headers=h)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"


def test_live_and_normal_counted_separately(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", "20")
    _use(ANON_A, minutes=20, mode="normal")
    assert _live(client, ANON_A).status_code == 200


# ----- Worker: zählt Gesprächsminuten, beendet bei Erreichen ---------------------
def _run_free(monkeypatch, *, room, humans, wait_s, live_mode=True):
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

    async def _go():
        await w.entrypoint(ctx)
        await asyncio.sleep(wait_s)

    asyncio.run(_go())
    return ctx, session


def test_worker_ends_free_call_at_limit_with_announcement(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", str(0.3 / 60))   # 0,3 s
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-1", "live", keys)
    ctx, session = _run_free(monkeypatch, room="free-1", humans=1, wait_s=1.0)
    session.generate_reply.assert_called_once()
    assert w.FREE_LIMIT_ANNOUNCEMENT in session.generate_reply.call_args.kwargs["instructions"]
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    ctx.delete_room.assert_awaited_once_with("free-1")
    assert freetier.used_seconds(keys, "live") >= 0.3
    assert len(w.FREE_LIMIT_ANNOUNCEMENT) <= 60 and "Guthaben" in w.FREE_LIMIT_ANNOUNCEMENT


def test_worker_normal_mode_announces_via_say(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", str(0.2 / 60))
    freetier.register_room("free-n", "normal", freetier.identity_keys(ANON_A, "1.1.1.1"))
    ctx, session = _run_free(monkeypatch, room="free-n", humans=1, wait_s=0.8, live_mode=False)
    session.say.assert_called_once_with(w.FREE_LIMIT_ANNOUNCEMENT, allow_interruptions=False)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_under_limit_keeps_running_and_books_minutes(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("free-2", "live", keys)                # Default 20 min
    ctx, session = _run_free(monkeypatch, room="free-2", humans=1, wait_s=0.3)
    ctx.shutdown.assert_not_called()                              # Positivkontrolle
    assert 0.1 < freetier.used_seconds(keys, "live") < 5          # Zeit mit Mensch gebucht


def test_worker_counts_no_time_without_humans(monkeypatch):
    keys = freetier.identity_keys(ANON_B, "3.3.3.3")
    freetier.register_room("free-3", "live", keys)
    _run_free(monkeypatch, room="free-3", humans=0, wait_s=0.3)
    assert freetier.used_seconds(keys, "live") == 0


def test_worker_parallel_rooms_share_ip_quota(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", str(0.3 / 60))
    _use(ANON_B, ip="1.1.1.1", minutes=0.3 / 60)                   # anderer Raum derselben IP: voll
    freetier.register_room("free-4", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))
    ctx, _ = _run_free(monkeypatch, room="free-4", humans=1, wait_s=0.3)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_paid_room_is_not_tracked(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", str(0.1 / 60))
    db.record_stripe_session("cs_wk", "", 1000)
    acc = db.claim_session("cs_wk")[1]
    db.bind_room("paid-1", acc, "live")
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("paid-1", "live", keys)               # selbst wenn ein Eintrag da wäre
    ctx, _ = _run_free(monkeypatch, room="paid-1", humans=1, wait_s=0.4)
    ctx.shutdown.assert_not_called()
    assert freetier.used_seconds(keys, "live") == 0


def test_worker_admin_room_exempt_is_not_limited(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", str(0.1 / 60))
    freetier.register_room("admin-room", "live", [], exempt=True)
    ctx, _ = _run_free(monkeypatch, room="admin-room", humans=1, wait_s=0.4)
    ctx.shutdown.assert_not_called()
