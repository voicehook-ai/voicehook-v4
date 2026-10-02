"""Globaler Gratis-Deckel (Oliver 02.10., intern): Monatstopf VH_FREE_POT_EUR_MONTH (60 EUR)
in ECHTEN Kosten, verteilt auf die Resttage des UTC-Monats. Heute leer -> Gratis für alle
leer bis zum nächsten UTC-Tag, dieselbe 402-Meldung wie beim persönlichen Limit.
Auf origin/main gibt es weder den Topf noch die Admin-Sicht: diese Datei schlägt dort fehl
(Positivkontrolle)."""

from __future__ import annotations

import calendar
import json
import time

import pytest
from fastapi.testclient import TestClient

from agent import freetier
from agent.billing import db
from agent.server import app

from .test_billing import _paid_wallet
from .test_freetier import (  # noqa: F401  (Fixture _env wirkt autouse über den Import)
    ANON_A,
    ANON_B,
    _env,
    _live,
    _rt,
    _run_free,
)

E = 1_000_000  # µEUR pro EUR


@pytest.fixture
def client():
    return TestClient(app)
LIVE_KEY = "live-key-test-do-not-use"


def _ts(y, m, d, h=12):
    return calendar.timegm((y, m, d, h, 0, 0))


def _drain_today(leave_ueur: int = 0) -> None:
    """Topf heute bis auf `leave_ueur` leeren (echte Kosten)."""
    left = freetier.pot_status()["left_today_ueur"]
    freetier.add_pot_ueur(left - leave_ueur)
    assert freetier.pot_left_ueur() == leave_ueur


# ----- Formel ----------------------------------------------------------------------
def test_default_pot_60_eur(monkeypatch):
    monkeypatch.delenv("VH_FREE_POT_EUR_MONTH", raising=False)
    assert freetier.pot_eur_month() == 60.0


@pytest.mark.parametrize("bad", ["kaputt", "-1", "inf", "nan"])
def test_pot_env_broken_is_zero_locked(monkeypatch, client, bad):
    monkeypatch.setenv("VH_FREE_POT_EUR_MONTH", bad)
    assert freetier.pot_eur_month() == 0.0 and freetier.pot_left_ueur() == 0
    assert _live(client, ANON_A).status_code == 402                 # nie unbegrenzt


def test_formula_month_start_is_60_over_days():
    assert freetier.pot_budget_today_ueur(60 * E, 0, 30) == 2 * E      # 30-Tage-Monat: 2 EUR
    st = freetier.pot_status(now=_ts(2026, 9, 1))
    assert st["days_left"] == 30 and st["budget_today_ueur"] == 2 * E
    st = freetier.pot_status(now=_ts(2026, 10, 1))                  # Oktober: 31 Tage
    assert st["days_left"] == 31 and st["budget_today_ueur"] == 60 * E // 31


def test_formula_mid_month_underuse_spreads_rest():
    """Monatsmitte mit Unterverbrauch: nicht Genutztes verteilt sich auf die Resttage."""
    for d in range(1, 15):                                          # 14 Tage je 0,50 EUR = 7 EUR
        freetier.add_pot_ueur(E // 2, now=_ts(2026, 10, d))
    st = freetier.pot_status(now=_ts(2026, 10, 15))
    assert st["days_left"] == 17
    assert st["month_used_ueur"] == 7 * E
    assert st["budget_today_ueur"] == (60 - 7) * E // 17 > 60 * E // 31   # mehr als Start
    freetier.add_pot_ueur(E, now=_ts(2026, 10, 15, 9))              # heutiger Verbrauch ...
    st = freetier.pot_status(now=_ts(2026, 10, 15, 20))
    assert st["budget_today_ueur"] == (60 - 7) * E // 17             # ... ändert das Tagesbudget nicht
    assert st["today_used_ueur"] == E and st["left_today_ueur"] == (60 - 7) * E // 17 - E


def test_formula_last_day_gets_all_rest():
    freetier.add_pot_ueur(55 * E, now=_ts(2026, 10, 5))
    st = freetier.pot_status(now=_ts(2026, 10, 31))
    assert st["days_left"] == 1 and st["budget_today_ueur"] == 5 * E


def test_formula_previous_month_does_not_count():
    freetier.add_pot_ueur(59 * E, now=_ts(2026, 9, 30))
    assert freetier.pot_status(now=_ts(2026, 10, 1))["budget_today_ueur"] == 60 * E // 31


def test_formula_overspent_month_gives_zero():
    assert freetier.pot_budget_today_ueur(60 * E, 61 * E, 3) == 0


# ----- Sperre für alle / neuer Tag ----------------------------------------------------
def test_empty_pot_blocks_everyone_with_plain_free_limit(client):
    assert _live(client, ANON_A).status_code == 200                 # Positivkontrolle
    _drain_today()
    for anon, ip in ((ANON_A, "1.1.1.1"), (ANON_B, "2.2.2.2"), (None, "3.3.3.3")):
        r = _live(client, anon, ip=ip)
        assert r.status_code == 402
        assert r.json()["detail"] == {"error": "free_limit", "topup_url": "/aufladen",
                                      "free_eur_per_day": 1.0}       # gleiche Meldung wie persönlich
        h = {"x-forwarded-for": ip, **({"x-anon-id": anon} if anon else {})}
        assert client.get("/api/me", headers=h).json()["free"] == {"eur_left": 0.0, "eur_per_day": 1.0}
    r = client.post("/api/host-call", json={"identity": "u"},
                    headers={"x-forwarded-for": "4.4.4.4", "x-anon-id": ANON_B})
    assert r.status_code == 402                                     # Normal genauso


def test_402_and_me_never_mention_pot(client):
    _drain_today()
    h = {"x-forwarded-for": "1.1.1.1", "x-anon-id": ANON_A}
    texts = [json.dumps(_live(client, ANON_A).json()).lower(),
             json.dumps(client.get("/api/me", headers=h).json()).lower(),
             json.dumps(client.get("/api/free/remaining", headers=h).json()).lower()]
    for t in texts:
        for word in ("pot", "topf", "marketing", "budget", "month", "monat", "global"):
            assert word not in t


def test_new_utc_day_gives_budget_again():
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    d15, d16 = _ts(2026, 10, 15), _ts(2026, 10, 16)
    b15 = freetier.pot_status(now=d15)["budget_today_ueur"]
    freetier.add_pot_ueur(b15, now=d15)
    assert freetier.remaining_ueur(keys, now=d15) == 0              # gestern leer
    assert freetier.pot_status(now=d16)["budget_today_ueur"] == (60 * E - b15) // 16
    assert freetier.remaining_ueur(keys, now=d16) == 1 * E          # neuer Tag: wieder Gratis


def test_consume_books_real_cost_proportionally_until_pot_empty():
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    _drain_today(leave_ueur=1_000)
    # Kundenpreis 10 000, echte Kosten 4 000; Topf hat nur noch 1 000 echt -> 1/4 gedeckt
    taken, rest = freetier.consume_ueur(keys, 10_000, real_ueur=4_000)
    assert (taken, rest) == (2_500, 0)
    assert freetier.pot_left_ueur() == 0 and freetier.used_ueur(keys) == 2_500
    assert freetier.consume_ueur(keys, 10_000, real_ueur=4_000) == (0, 0)


# ----- Owner -------------------------------------------------------------------------
def test_owner_not_blocked_and_books_nothing(monkeypatch, client):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    monkeypatch.setenv("VH_FREE_EXEMPT_KEYS", ",".join(keys))
    _drain_today()
    assert _live(client, ANON_A).status_code == 200
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 402   # andere weiter gesperrt
    before = freetier.pot_status()["today_used_ueur"]
    assert freetier.consume_ueur(keys, 50_000, real_ueur=20_000) == (50_000, 1 * E)
    assert freetier.pot_status()["today_used_ueur"] == before


# ----- Worker: echte Kosten, Wallet, Ende -----------------------------------------------
def test_worker_books_real_cost_into_pot_not_customer_price(monkeypatch):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("pot-l1", "live", keys)
    ctx, _ = _run_free(monkeypatch, room="pot-l1", humans=1, wait_s=0.05, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_not_called()
    usd = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert freetier.used_ueur(keys) == 14_148                       # persönlich: Kundenpreis
    assert freetier.pot_status()["today_used_ueur"] == round(usd * 0.8807 * E) == 7_926  # Topf: echt


def test_worker_normal_mode_books_pot_too(monkeypatch):
    from .test_freetier import _stt
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("pot-n1", "normal", keys)
    _run_free(monkeypatch, room="pot-n1", humans=1, wait_s=0.05, live_mode=False, metrics=[_stt(60.0)])
    assert freetier.pot_status()["today_used_ueur"] == round(0.0077 * 0.8807 * E)


def test_worker_empty_pot_without_wallet_ends_call(monkeypatch):
    freetier.register_room("pot-end", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))
    _drain_today(leave_ueur=1_000)                                  # reicht nicht für 1 Ereignis
    ctx, _ = _run_free(monkeypatch, room="pot-end", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    assert freetier.pot_left_ueur() == 0


def test_worker_empty_pot_at_start_ends_call(monkeypatch):
    freetier.register_room("pot-end0", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))
    _drain_today()
    ctx, _ = _run_free(monkeypatch, room="pot-end0", humans=1, wait_s=0.2)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


def test_worker_empty_pot_wallet_keeps_paying(monkeypatch):
    db.record_stripe_session("cs_pot", "", 1000)
    acc = db.claim_session("cs_pot")[1]
    db.bind_room("pot-w", acc, "live")
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.register_room("pot-w", "live", keys)
    _drain_today(leave_ueur=1_000)
    ctx, session = _run_free(monkeypatch, room="pot-w", humans=1, wait_s=0.1,
                             metrics=[_rt(1000, 500), _rt(1000, 500)])
    ctx.shutdown.assert_not_called()                                # Call läuft weiter
    covered = 14_148 * 1_000 // 7_926                               # anteilig aus dem Topf
    assert freetier.used_ueur(keys) == covered
    assert db.balance_ueur(acc) == 10 * E - (14_148 - covered) - 14_148
    assert freetier.pot_left_ueur() == 0


# ----- fail-closed --------------------------------------------------------------------
def test_pot_read_error_is_fail_closed(monkeypatch, client):
    assert _live(client, ANON_A).status_code == 200                 # Positivkontrolle

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(freetier, "_pot_read", _boom)
    assert freetier.pot_left_ueur() == 0
    assert freetier.remaining_ueur(freetier.identity_keys(ANON_B, "2.2.2.2")) == 0
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 402
    with pytest.raises(RuntimeError):
        freetier.consume_ueur(freetier.identity_keys(ANON_B, "2.2.2.2"), 1_000, real_ueur=500)


def test_worker_pot_read_error_ends_call(monkeypatch):
    freetier.register_room("pot-err", "live", freetier.identity_keys(ANON_A, "1.1.1.1"))

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(freetier, "_pot_read", _boom)
    ctx, _ = _run_free(monkeypatch, room="pot-err", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


# ----- Live-Monatssperre bleibt zusätzlich ------------------------------------------------
def test_live_month_lock_still_applies_with_full_pot(monkeypatch, client):
    from agent import budget
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    budget.add_usd(1.0)
    assert freetier.pot_left_ueur() > 0
    assert _live(client, ANON_A).status_code == 402                 # strengere Sperre greift


# ----- Admin-Sicht ----------------------------------------------------------------------
def test_admin_free_pot_requires_bearer(monkeypatch, client):
    monkeypatch.delenv("VOICEHOOK_LIVE_KEY", raising=False)
    assert client.get("/api/admin/free-pot").status_code == 404
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    assert client.get("/api/admin/free-pot").status_code == 401
    assert client.get(f"/api/admin/free-pot?key={LIVE_KEY}").status_code == 401   # nie in der URL
    assert client.get("/api/admin/free-pot", headers={"authorization": "Bearer wrong"}).status_code == 401


def test_admin_free_pot_reports_state(monkeypatch, client):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)
    freetier.add_pot_ueur(250_000)
    r = client.get("/api/admin/free-pot", headers={"authorization": f"Bearer {LIVE_KEY}"})
    assert r.status_code == 200
    j = r.json()
    t = time.gmtime()
    days_left = calendar.monthrange(t.tm_year, t.tm_mon)[1] - t.tm_mday + 1
    assert j["month_budget_eur"] == 60.0 and j["days_left"] == days_left
    assert j["today_used_eur"] == 0.25 and j["month_used_eur"] >= 0.25
    assert j["left_today_eur"] == round(j["budget_today_eur"] - 0.25, 4)


def test_admin_free_pot_read_error_503(monkeypatch, client):
    monkeypatch.setenv("VOICEHOOK_LIVE_KEY", LIVE_KEY)

    def _boom(*a, **k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(freetier, "_pot_read", _boom)
    r = client.get("/api/admin/free-pot", headers={"authorization": f"Bearer {LIVE_KEY}"})
    assert r.status_code == 503


# ----- Wallet-Nutzer bei leerem Topf: Raum mit Wallet geht weiter ---------------------------
def test_empty_pot_paid_wallet_still_gets_room(client):
    _drain_today()
    wl = _paid_wallet(client, "cs_pot_room")
    assert _live(client, ANON_A, wallet=wl["wallet_token"]).status_code == 200
