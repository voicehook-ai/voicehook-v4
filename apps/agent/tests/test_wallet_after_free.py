"""Gratis aufgebraucht, Guthaben da (Oliver 02.10., iPhone: "Guthaben leer und voll
gleichzeitig"): auf JEDEM Pfad, der einen Raum anlegt (host-call = "Mit Delta sprechen",
invite-room = "Agent einladen", live-room = Live), startet der Call und läuft auf dem
Wallet, nie Call-Ende free_limit. Erst wenn Gratis UND Guthaben leer sind: 402 VOR der
Raumanlage (kein Slug, keine Bindung, kein Dispatch). Positivkontrolle je Pfad."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.server as srv
from agent import freetier
from agent.billing import db
from agent.server import app

from .test_billing import _paid_wallet
from .test_freetier import _rt, _run_free, _stt
from .test_review2 import _env  # noqa: F401  (autouse-Env: Stripe/LiveKit/Invite, kein Dispatch)
from .test_wallet_worker import _account

ANON = "anon-walletbug-0001"
IP = "9.9.9.9"
H = {"x-anon-id": ANON, "x-forwarded-for": IP}
PATHS = [("/api/host-call", "normal"), ("/api/invite-room", "normal"), ("/api/live-room", "live")]


def _free_used_up():
    freetier.add_ueur(freetier.identity_keys(ANON, IP), 10_000_000)
    assert freetier.remaining_ueur(freetier.identity_keys(ANON, IP)) == 0


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def slugs(monkeypatch):
    """Zählt Raumanlagen (gen_slug) und Dispatches: 402 darf keins von beidem auslösen."""
    made: list[str] = []
    real = srv.gen_slug

    def _gen():
        s = real()
        made.append(s)
        return s
    monkeypatch.setattr(srv, "gen_slug", _gen)
    return made


def _worker_mode(monkeypatch, mode):
    if mode == "live":
        monkeypatch.setenv("VOICEHOOK_LIVE_PUBLIC", "1")


@pytest.mark.parametrize("path,mode", PATHS)
def test_free_used_up_wallet_positive_call_starts_on_wallet(client, slugs, monkeypatch, path, mode):
    _worker_mode(monkeypatch, mode)
    _free_used_up()
    wl = _paid_wallet(client, f"cs_wab_{mode}_{path[-4:]}")
    acc = db.account_for_token(wl["wallet_token"])
    r = client.post(path, json={"identity": "host-x"}, headers={**H, "x-wallet-token": wl["wallet_token"]})
    assert r.status_code == 200, r.text
    room = r.json()["room"]
    assert slugs == [room]
    assert db.room_wallet(room) == (acc, mode)                       # Raum hängt am Wallet
    assert freetier.room_keys(room) is None                          # kein Gratis-Teil mehr

    before = db.balance_ueur(acc)
    metrics = [_rt(200, 100)] * 2 if mode == "live" else [_stt(30.0)] * 2
    ctx, _ = _run_free(monkeypatch, room=room, humans=1, wait_s=0.1,
                       live_mode=(mode == "live"), metrics=metrics)
    ctx.shutdown.assert_not_called()                                 # kein free_limit, kein Refuse
    assert db.balance_ueur(acc) < before                             # Wallet zahlt


@pytest.mark.parametrize("path,mode", PATHS)
def test_free_and_wallet_empty_402_before_room_exists(client, slugs, monkeypatch, path, mode):
    _worker_mode(monkeypatch, mode)
    _free_used_up()
    dispatched: list = []
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda *a, **k: dispatched.append(a))
    # ohne Wallet
    r = client.post(path, json={"identity": "host-x"}, headers=H)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"
    # Wallet mit Saldo 0
    acc = _account(500, f"cs_empty_{mode}_{path[-4:]}")
    db.charge(acc, db.balance_ueur(acc), room="x", mode="normal", usd=0.0)
    assert db.balance_ueur(acc) <= 0
    tok = db.issue_token(acc)
    r2 = client.post(path, json={"identity": "host-x"}, headers={**H, "x-wallet-token": tok})
    assert r2.status_code == 402 and r2.json()["detail"]["error"] == "free_limit"
    assert slugs == [] and dispatched == []                          # Raum nie angelegt
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM room_wallets").fetchone()[0] == 0
    conn.close()


def test_worker_free_pot_empty_at_start_with_wallet_continues(monkeypatch):
    """Raum mit Gratis-Merkmalen UND Wallet, Topf ist beim Start schon leer (z. B. ein
    zweiter Tab hat ihn zwischen Anlage und Worker-Start verbraucht): Wallet zahlt."""
    keys = freetier.identity_keys(ANON, IP)
    acc = _account(1000, "cs_wab_start")
    db.bind_room("wab-start", acc, "normal")
    freetier.register_room("wab-start", "normal", keys)
    _free_used_up()
    ctx, _ = _run_free(monkeypatch, room="wab-start", humans=1, wait_s=0.1, live_mode=False,
                       metrics=[_stt(30.0)])
    ctx.shutdown.assert_not_called()
    assert db.balance_ueur(acc) < 10_000_000


def test_worker_free_pot_empty_at_start_without_wallet_ends_free_limit(monkeypatch):
    """Positivkontrolle: derselbe Raum ohne Wallet endet mit free_limit."""
    keys = freetier.identity_keys(ANON, IP)
    freetier.register_room("wab-nowallet", "normal", keys)
    _free_used_up()
    ctx, _ = _run_free(monkeypatch, room="wab-nowallet", humans=1, wait_s=0.1, live_mode=False)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_voice_html_call_button_follows_every_call_start():
    """Root Cause 02.10.: vhStartCall (Join-Gate, /r/<slug>?invite=, Modell-Modal) setzte
    den Haupt-Knopf nicht auf "Auflegen"; er stand weiter auf "Agent einladen", der
    nächste Tipp darauf legte den laufenden Call nach 3 s auf. Und die Info-Zeile sagt,
    wovon der Call bezahlt wird."""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[3] / "web/voice.html").read_text()
    start = html.index("async function vhStartCall()")
    body = html[start:html.index("function vhEndCall(", start)]
    assert "vhSetCallUi('in-call')" in body
    assert "'free.wallet': 'Gratis aufgebraucht · zahlt aus Guthaben {v}'" in html
