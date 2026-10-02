"""Shared pytest setup — fake API keys so factories construct in CI without real creds."""

from __future__ import annotations

import os

# Apply once at import-time (before any test module imports a livekit plugin).
os.environ.setdefault("DEEPGRAM_API_KEY", "test-deepgram-key-do-not-use")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key-do-not-use")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_live_budget(tmp_path, monkeypatch):
    """Kein Test liest/schreibt je das echte Budget-Ledger der Box."""
    monkeypatch.setenv("VOICEHOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", raising=False)
    # Die Alt-Tests rechnen mit 1 EUR Gratis pro Tag als Einheit; der Code-Default ist
    # seit 02.10. 0,30 EUR (eigene Tests in test_free_pot.py / test_freetier.py).
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "1.0")
    monkeypatch.delenv("VH_FREE_POT_EUR_MONTH", raising=False)
