"""Worker bucht echten Verbrauch x Faktor vom Wallet am Raum; Saldo 0 -> Ansage + Ende."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agent.worker as w
from agent import budget
from agent.billing import db, pricing

from .test_worker import _Emitter, _metric


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("VOICEHOOK_PRICE_FACTOR_NORMAL", "VOICEHOOK_PRICE_FACTOR_LIVE", "VOICEHOOK_VAT_RATE",
              "VOICEHOOK_USD_EUR"):
        monkeypatch.delenv(k, raising=False)


def _account(cents: int, sid: str = "cs_w") -> str:
    db.record_stripe_session(sid, f"{sid}@x.y", cents)
    return db.claim_session(sid)[1]


def _run(monkeypatch, *, live_mode, metrics, room="r1"):
    if live_mode:
        monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
    else:
        monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
        monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
    session = _Emitter()
    session.start = AsyncMock()
    session.aclose = AsyncMock()
    session.say = MagicMock(return_value=SimpleNamespace(wait_for_playout=AsyncMock()))
    session.generate_reply = MagicMock(return_value=SimpleNamespace(wait_for_playout=AsyncMock()))
    built = []

    def _build():
        built.append(1)
        return session

    monkeypatch.setattr(w, "build_session", _build)
    r = _Emitter()
    r.name = room
    r.remote_participants = {}
    r.local_participant = SimpleNamespace(identity="voice-ai", publish_data=AsyncMock())
    ctx = SimpleNamespace(connect=AsyncMock(), room=r, job=SimpleNamespace(id="j1"),
                          shutdown=MagicMock(), delete_room=AsyncMock())

    async def _go():
        await w.entrypoint(ctx)
        for m in metrics:
            session.emit("metrics_collected", SimpleNamespace(metrics=m))
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_go())
    return ctx, session, built


def test_normal_call_charges_factor_3(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "normal")
    stt = _metric("STTMetrics", audio_duration=60.0)          # 0.0077 USD
    _run(monkeypatch, live_mode=False, metrics=[stt])
    expected = round(0.0077 * pricing.DEFAULT_USD_EUR * 3 * 1.19 * 1_000_000)
    assert db.balance_ueur(acc) == 10_000_000 - expected


def test_live_call_charges_factor_1_5_and_budget_unchanged(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "live")
    rt = _metric("RealtimeModelMetrics", input_tokens=1000, output_tokens=500,
                 input_token_details=None, output_token_details=None)
    _run(monkeypatch, live_mode=True, metrics=[rt])
    usd = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert db.balance_ueur(acc) == 10_000_000 - round(usd * pricing.DEFAULT_USD_EUR * 1.5 * 1.19 * 1e6)
    assert budget.spent_usd() == pytest.approx(usd)            # Live-Monatsbudget bucht weiter


def test_room_without_wallet_is_not_charged(monkeypatch):
    acc = _account(1000)
    db.bind_room("anderer-raum", acc, "normal")
    ctx, _, _ = _run(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=600.0)])
    assert db.balance_ueur(acc) == 10_000_000
    ctx.shutdown.assert_not_called()


def test_balance_hits_zero_announces_and_ends_once(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "normal")
    db.charge(acc, 10_000_000 - 100, room="r1", mode="normal", usd=0)   # 0,0001 EUR übrig
    stt = _metric("STTMetrics", audio_duration=10.0)
    ctx, session, _ = _run(monkeypatch, live_mode=False, metrics=[stt, stt])
    assert db.balance_ueur(acc) == 0
    session.say.assert_called_once_with(w.WALLET_EMPTY_ANNOUNCEMENT, allow_interruptions=False)
    ctx.shutdown.assert_called_once_with(reason="call_guard:wallet_empty")
    assert len(w.WALLET_EMPTY_ANNOUNCEMENT) <= 60               # kurze Ansage (TTS-Grenze)


def test_balance_positive_keeps_call_running(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "normal")
    ctx, session, _ = _run(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=10.0)])
    session.say.assert_not_called()
    ctx.shutdown.assert_not_called()                              # Positivkontrolle zum Test oben


def test_empty_wallet_refuses_room_before_session(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "normal")
    db.charge(acc, 10**9, room="r1", mode="normal", usd=0)
    ctx, _, built = _run(monkeypatch, live_mode=False, metrics=[])
    assert built == []
    ctx.shutdown.assert_called_once_with(reason="wallet_empty")


def test_charger_fails_closed_on_db_error(monkeypatch):
    acc = _account(1000)
    db.bind_room("r1", acc, "normal")
    c = w.WalletCharger("r1", "normal")
    assert c.lookup() == acc

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(db, "charge", boom)
    assert c.charge(0.01) is True
    assert c.charge(0.01) is False          # nur einmal melden
