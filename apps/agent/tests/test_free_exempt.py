"""VH_FREE_EXEMPT_KEYS (Oliver 01.10.: "nimm das limit für mich raus"): gehashte Merkmale
ohne Gratis-Limit. Kein 402 free_limit, kein Call-Ende, keine Buchung; andere Merkmale
zählen normal; leere/kaputte Env = keine Ausnahme."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.worker as w
from agent import freetier
from agent.server import app

from .test_freetier import (  # noqa: F401  (Fixture _env wirkt autouse über den Import)
    ANON_A,
    ANON_B,
    _env,
    _live,
    _rt,
    _run_free,
    _use,
)

OWNER_IP = "1.1.1.1"
ENV = "VH_FREE_EXEMPT_KEYS"


def _owner_keys():
    return freetier.identity_keys(ANON_A, OWNER_IP)


def _exempt(monkeypatch, value):
    monkeypatch.setenv(ENV, value)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_exempt(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


# ----- Parsing -----------------------------------------------------------------
def test_parse_ignores_broken_entries(monkeypatch):
    anon, ip = _owner_keys()
    _exempt(monkeypatch, f" {anon} ,, kaputt, ip:123, 1.1.1.1, {ip.upper()}")
    assert freetier.exempt_keys() == frozenset({anon, ip})


def test_empty_env_no_exemption(monkeypatch):
    _exempt(monkeypatch, "")
    assert freetier.exempt_keys() == frozenset() and not freetier.is_exempt(_owner_keys())
    _exempt(monkeypatch, " , ,kaputt")
    assert not freetier.is_exempt(_owner_keys())


# ----- HTTP: kein 402, /api/me ----------------------------------------------------
def test_positive_control_without_env_is_402(client):
    _use(ANON_A, ip=OWNER_IP)
    assert _live(client, ANON_A, ip=OWNER_IP).status_code == 402


def test_exempt_key_gets_no_402(client, monkeypatch):
    _use(ANON_A, ip=OWNER_IP)                                     # Topf heute voll
    _exempt(monkeypatch, _owner_keys()[1])                        # nur der IP-Key reicht
    r = _live(client, ANON_A, ip=OWNER_IP)
    assert r.status_code == 200
    assert freetier.room_keys(r.json()["room"])[1] == _owner_keys()


def test_other_key_still_counted(client, monkeypatch):
    _exempt(monkeypatch, _owner_keys()[0])                        # nur Owner-Anon-ID
    _use(ANON_B, ip="2.2.2.2")
    assert _live(client, ANON_B, ip="2.2.2.2").status_code == 402
    assert freetier.remaining_ueur(freetier.identity_keys(ANON_B, "2.2.2.2")) == 0


def test_me_reports_full_and_exempt_flag(client, monkeypatch):
    _use(ANON_A, ip=OWNER_IP)
    h = {"x-forwarded-for": OWNER_IP, "x-anon-id": ANON_A}
    assert client.get("/api/me", headers=h).json()["free"] == {"eur_left": 0.0, "eur_per_day": 1.0}
    _exempt(monkeypatch, ",".join(_owner_keys()))
    assert client.get("/api/me", headers=h).json()["free"] == {
        "eur_left": 1.0, "eur_per_day": 1.0, "exempt": True}
    other = {"x-forwarded-for": "2.2.2.2", "x-anon-id": ANON_B}
    assert "exempt" not in client.get("/api/me", headers=other).json()["free"]


# ----- Worker: keine Buchung, kein Ende ------------------------------------------
def test_worker_exempt_no_booking_no_end(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")             # 1 Ereignis würde reichen
    _exempt(monkeypatch, ",".join(_owner_keys()))
    keys = _owner_keys()
    freetier.register_room("own-1", "live", keys)
    ctx, _ = _run_free(monkeypatch, room="own-1", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)] * 3)
    ctx.shutdown.assert_not_called()
    assert freetier.used_ueur(keys) == 0                          # nichts gebucht


def test_worker_exempt_even_if_pot_full_at_start(monkeypatch):
    _use(ANON_A, ip=OWNER_IP)
    _exempt(monkeypatch, _owner_keys()[0])
    freetier.register_room("own-2", "live", _owner_keys())
    ctx, _ = _run_free(monkeypatch, room="own-2", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_not_called()


def test_worker_positive_control_other_key_ends(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    _exempt(monkeypatch, ",".join(_owner_keys()))
    keys = freetier.identity_keys(ANON_B, "2.2.2.2")
    freetier.register_room("oth-1", "live", keys)
    ctx, _ = _run_free(monkeypatch, room="oth-1", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    assert freetier.used_ueur(keys) == 10_000
    assert w.FREE_LIMIT_ANNOUNCEMENT


# ----- CLI -----------------------------------------------------------------------
def test_cli_keys_prints_identity_keys(capsys):
    assert freetier._main(["keys", "--ip", OWNER_IP, "--anon", ANON_A]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == _owner_keys()
    assert out[2] == f"VH_FREE_EXEMPT_KEYS={','.join(_owner_keys())}"
    assert OWNER_IP not in "".join(out) and ANON_A not in "".join(out)   # nur Hashes


def test_cli_ipv6_uses_64_bucket(capsys):
    freetier._main(["keys", "--ip", "2001:db8::1"])
    a = capsys.readouterr().out.splitlines()[0]
    freetier._main(["keys", "--ip", "2001:db8::ffff"])
    assert capsys.readouterr().out.splitlines()[0] == a


def test_cli_requires_input():
    with pytest.raises(SystemExit):
        freetier._main(["keys"])
