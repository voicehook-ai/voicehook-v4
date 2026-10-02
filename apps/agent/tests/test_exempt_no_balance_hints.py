"""Owner-Ausnahme (VH_FREE_EXEMPT_KEYS, Oliver 02.10.: "Nimm das mal gerade raus bei
mir, dieses Guthaben-Gedöns"): Räume des Owners bekommen keine Low-Balance-Warnung
(kein operator.notice low_balance, keine Ansage), im Normal- und im Live-Modus. Die
Live-Monatssperre (budget.py) gilt weiter samt Ansage. Andere Nutzer: wie bisher."""

from __future__ import annotations

import json

import pytest

import agent.worker as w
from agent import freetier, relay

from .test_freetier import (  # noqa: F401  (Fixture _env wirkt autouse über den Import)
    ANON_A,
    ANON_B,
    _env,
    _rt,
    _run_free,
)
from .test_worker import _metric

ENV = "VH_FREE_EXEMPT_KEYS"
OWNER = freetier.identity_keys(ANON_A, "1.1.1.1")
OTHER = freetier.identity_keys(ANON_B, "2.2.2.2")


@pytest.fixture(autouse=True)
def _fast_watch(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    # Topf 0,01 EUR: bei diesem Verbrauch reicht er < 5 min, die Warnung wäre fällig.
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    real = w.LowBalanceWatch.__init__

    def _fast(self, *a, **k):
        real(self, *a, **{**k, "min_span_s": 0.05})                 # Hochrechnung nach 50 ms
    monkeypatch.setattr(w.LowBalanceWatch, "__init__", _fast)


def _owner_env(monkeypatch):
    monkeypatch.setenv(ENV, ",".join(freetier.identity_keys(ANON_A, "1.1.1.1")))


def _notices(ctx):
    pub = ctx.room.local_participant.publish_data
    return [json.loads(c.kwargs["payload"]) for c in pub.call_args_list
            if c.kwargs.get("topic") == relay.TOPIC_NOTICE]


def _said(session):
    return [c.args[0] for c in session.say.call_args_list] + [
        str(c.kwargs.get("instructions", "")) for c in session.generate_reply.call_args_list]


def _run(monkeypatch, room, keys, live_mode):
    freetier.register_room(room, "live" if live_mode else "normal", keys)
    metrics = [_rt(20, 10)] * 4 if live_mode else [_metric("STTMetrics", audio_duration=1.0)] * 4
    return _run_free(monkeypatch, room=room, humans=1, wait_s=0.3, live_mode=live_mode,
                     metrics=metrics, gap_s=0.05)


@pytest.mark.parametrize("live_mode", [False, True])
def test_owner_room_no_low_balance_notice_no_announcement(monkeypatch, live_mode):
    _owner_env(monkeypatch)
    ctx, session = _run(monkeypatch, f"own-lb-{live_mode}", OWNER, live_mode)
    assert not [n for n in _notices(ctx) if n.get("kind") == "low_balance"]
    assert not any(relay.LOW_BALANCE_ANNOUNCEMENT in s for s in _said(session))
    ctx.shutdown.assert_not_called()


@pytest.mark.parametrize("live_mode", [False, True])
def test_positive_control_other_room_still_warned(monkeypatch, live_mode):
    _owner_env(monkeypatch)
    ctx, session = _run(monkeypatch, f"oth-lb-{live_mode}", OTHER, live_mode)
    assert [n for n in _notices(ctx) if n.get("kind") == "low_balance"]
    assert any(relay.LOW_BALANCE_ANNOUNCEMENT in s for s in _said(session))


@pytest.mark.parametrize("live_mode", [False, True])
def test_positive_control_owner_without_env_is_warned(monkeypatch, live_mode):
    ctx, session = _run(monkeypatch, f"own-noenv-{live_mode}", OWNER, live_mode)
    assert [n for n in _notices(ctx) if n.get("kind") == "low_balance"]


def test_owner_live_budget_still_ends_with_announcement(monkeypatch):
    _owner_env(monkeypatch)
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "0.0001")
    ctx, session = _run(monkeypatch, "own-budget", OWNER, live_mode=True)
    ctx.shutdown.assert_called_once_with(reason="call_guard:live_budget")
    assert any(w.LIVE_BUDGET_ANNOUNCEMENT in s for s in _said(session))
    assert not [n for n in _notices(ctx) if n.get("kind") == "low_balance"]


def test_free_budget_owner_flag(monkeypatch):
    _owner_env(monkeypatch)
    freetier.register_room("f-own", "live", OWNER)
    freetier.register_room("f-oth", "live", OTHER)
    freetier.register_room("f-adm", "live", [], exempt=True)
    owner = w.FreeBudget("f-own", "live")
    owner.lookup()
    other = w.FreeBudget("f-oth", "live")
    other.lookup()
    adm = w.FreeBudget("f-adm", "live")
    adm.lookup()
    assert owner.owner is True and other.owner is False and adm.owner is False
