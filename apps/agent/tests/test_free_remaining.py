"""GET /api/free/remaining + /api/billing/config approx_eur_per_hour (UI-Runde 3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import freetier
from agent.server import app

ANON_A = "anon-aaaaaaaa-1111"
ANON_B = "anon-bbbbbbbb-2222"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "VOICEHOOK_FREE_MIN_PER_DAY_NORMAL",
              "VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL", "VOICEHOOK_APPROX_EUR_PER_HOUR_LIVE"):
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


def test_defaults_full_live_normal_off(client):
    assert _rem(client, ANON_A) == {"live_s": 1200, "normal_s": 0,
                                    "enabled": {"live": True, "normal": False}}


def test_used_minutes_reduce_remaining_per_mode(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_NORMAL", "10")
    freetier.add_seconds(freetier.identity_keys(ANON_A, "1.1.1.1"), "live", 450)
    freetier.add_seconds(freetier.identity_keys(ANON_A, "1.1.1.1"), "normal", 60)
    got = _rem(client, ANON_A)
    assert got == {"live_s": 750, "normal_s": 540, "enabled": {"live": True, "normal": True}}


def test_counts_like_host_call_either_anon_or_ip(client):
    freetier.add_seconds(freetier.identity_keys(ANON_A, "1.1.1.1"), "live", 600)
    assert _rem(client, ANON_A, ip="2.2.2.2")["live_s"] == 600   # gleiche Anon-ID, neue IP
    assert _rem(client, ANON_B, ip="1.1.1.1")["live_s"] == 600   # neue Anon-ID, gleiche IP
    assert _rem(client, ANON_B, ip="2.2.2.2")["live_s"] == 1200  # Positivkontrolle: beides frisch
    # IP wie bei host-call: letztes X-Forwarded-For-Element, Fake davor zählt nicht
    assert _rem(client, ANON_B, ip="9.9.9.9, 1.1.1.1")["live_s"] == 600


def test_never_negative_and_read_only(client):
    keys = freetier.identity_keys(ANON_A, "1.1.1.1")
    freetier.add_seconds(keys, "live", 5000)
    assert _rem(client, ANON_A)["live_s"] == 0
    before = freetier.used_seconds(keys, "live")
    _rem(client, ANON_A)
    assert freetier.used_seconds(keys, "live") == before          # Lesen bucht nichts


def test_mode_off_reports_disabled(client, monkeypatch):
    monkeypatch.setenv("VOICEHOOK_FREE_MIN_PER_DAY_LIVE", "0")
    got = _rem(client, ANON_A)
    assert got["enabled"]["live"] is False and got["live_s"] == 0


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
