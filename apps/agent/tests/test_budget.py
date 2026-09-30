"""Live-Monatsbudget: buchen, Monatswechsel, fail-closed."""

from __future__ import annotations

import calendar

from agent import budget

SEPT = calendar.timegm((2026, 9, 30, 12, 0, 0))
OCT = calendar.timegm((2026, 10, 1, 0, 0, 1))


def test_fresh_ledger_is_empty_and_open():
    assert budget.spent_usd(now=SEPT) == 0.0
    assert not budget.exhausted(now=SEPT)


def test_add_accumulates_and_blocks_at_limit(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    assert budget.add_usd(0.6, now=SEPT) == 0.6
    assert not budget.exhausted(now=SEPT)
    assert budget.add_usd(0.4, now=SEPT) == 1.0
    assert budget.exhausted(now=SEPT)


def test_new_month_starts_at_zero(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    budget.add_usd(5, now=SEPT)
    assert budget.exhausted(now=SEPT)
    assert not budget.exhausted(now=OCT)
    assert budget.spent_usd(now=OCT) == 0.0


def test_default_limit_is_ten_dollars():
    assert budget.limit_usd() == 10.0


def test_negative_amounts_never_refund():
    budget.add_usd(2, now=SEPT)
    budget.add_usd(-5, now=SEPT)
    assert budget.spent_usd(now=SEPT) == 2.0


def test_corrupt_ledger_fails_closed():
    p = budget.ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{kaputt")
    assert budget.exhausted(now=SEPT)
    assert budget.add_usd(0.1, now=SEPT) == float("inf")


def test_invalid_limit_fails_closed(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "zehn")
    assert budget.exhausted(now=SEPT)
