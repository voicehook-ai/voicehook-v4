"""GET /api/free/remaining (Gratis-Euro, Normal + Live gemeinsam) + /api/billing/config
approx_eur_per_hour (UI-Runde 3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import freetier
from agent.server import app

ANON_A = "anon-aaaaaaaa-1111"
ANON_B = "anon-bbbbbbbb-2222"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL", "VOICEHOOK_APPROX_EUR_PER_HOUR_LIVE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def client():
    return TestClient(app)


def _rem(client, anon=None, ip="1.1.1.1"):
    h = {"x-forwarded-for": ip}
    if anon:
        h["x-anon-id"] = anon
    r = client.get("/api/free/remaining", headers=h)
    assert r.status_code == 200
    return r.json()


def test_full_one_euro(client):
    assert _rem(client, ANON_A) == {"enabled": True, "eur_left": 1.0, "eur_per_day": 1.0}


def test_default_full_030_euro(client, monkeypatch):
    monkeypatch.delenv("VH_FREE_EUR_PER_DAY", raising=False)
    assert _rem(client, ANON_A) == {"enabled": True, "eur_left": 0.3, "eur_per_day": 0.3}


def test_used_euro_reduces_remaining_rounded_down(client):
    freetier.add_ueur(freetier.identity_keys(ANON_A, "1.1.1.1"), 333_333)
    assert _rem(client, ANON_A)["eur_left"] == 0.66                 # 0,666667 -> 0,66, nie mehr als da


def test_counts_like_host_call_either_anon_or_ip(client):
    freetier.add_ueur(freetier.identity_keys(ANON_A, "1.1.1.1"), 500_000)
    assert _rem(client, ANON_A, ip="2.2.2.2")["eur_left"] == 0.5   # gleiche Anon-ID, neue IP
    assert _rem(client, ANON_B, ip="1.1.1.1")["eur_left"] == 0.5   # neue Anon-ID, gleiche IP
    assert _rem(client, ANON_B, ip="2.2.2.2")["eur_left"] == 1.0   # Positivkontrolle: beides frisch
    # IP wie bei host-call: letztes X-Forwarded-For-Element, Fake davor zählt nicht
    assert _rem(client, ANON_B, ip="9.9.9.9, 1.1.1.1")["eur_left"] == 0.5


def test_never_negative_and_read_only(client):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.add_ueur(keys, 5_000_000)
    assert _rem(client, ANON_A)["eur_left"] == 0
    before = freetier.used_ueur(keys)
    _rem(client, ANON_A)
    assert freetier.used_ueur(keys) == before                      # Lesen bucht nichts


def test_off_reports_disabled(client, monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0")
    assert _rem(client, ANON_A) == {"enabled": False, "eur_left": 0.0, "eur_per_day": 0.0}


def test_billing_config_approx_price_defaults_and_env(client, monkeypatch):
    cfg = client.get("/api/billing/config").json()
    assert cfg["approx_eur_per_hour"] == {"normal": 1.70, "live": 8.80}
    monkeypatch.setenv("VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL", "2,5")
    monkeypatch.setenv("VOICEHOOK_APPROX_EUR_PER_HOUR_LIVE", "9.999")
    cfg = client.get("/api/billing/config").json()
    assert cfg["approx_eur_per_hour"] == {"normal": 2.5, "live": 10.0}
    for bad in ("kaputt", "-1", "nan", "inf"):
        monkeypatch.setenv("VOICEHOOK_APPROX_EUR_PER_HOUR_LIVE", bad)
        assert client.get("/api/billing/config").json()["approx_eur_per_hour"]["live"] == 8.80
    # bestehende Felder bleiben unverändert
    assert {"currency", "amounts_eur", "min_eur", "max_eur", "checkout_available"} <= set(cfg)
