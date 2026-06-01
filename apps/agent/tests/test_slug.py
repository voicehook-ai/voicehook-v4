"""Canonical slug SSOT — generation, validation, and the client⇄server invariant.

If web/voice.html's VH_SLUG_RX ever drifts from slug.SLUG_RE, the client
auto-join silently rejects server-minted slugs — the 2026-05-29 dead-call bug
(`nvu-GKJM` was minted but the client refused it). This test fails loud on drift.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent.slug import SLUG_RE, gen_slug, is_valid_slug

_VOICE_HTML = Path(__file__).resolve().parents[3] / "web" / "voice.html"


def test_gen_slug_always_matches_canonical_regex():
    for _ in range(100):
        assert SLUG_RE.match(gen_slug())


def test_gen_slug_is_random():
    assert len({gen_slug() for _ in range(50)}) > 1


def test_is_valid_slug_accepts_good_and_rejects_bad():
    assert is_valid_slug("drift-signal-crisp-PDM5")
    assert not is_valid_slug("nvu-GKJM")              # the 2026-05-29 dead-call shape
    assert not is_valid_slug("two-words-AB12")        # only two words
    assert not is_valid_slug("drift-signal-crisp-pdm5")  # lowercase suffix
    assert not is_valid_slug("drift-signal-crisp-AB")    # suffix too short


def test_client_regex_matches_server_ssot():
    """web/voice.html VH_SLUG_RX must be byte-for-byte equal to SLUG_RE.pattern."""
    html = _VOICE_HTML.read_text(encoding="utf-8")
    m = re.search(r"VH_SLUG_RX\s*=\s*/(?P<pat>.+?)/\s*;", html)
    assert m, "VH_SLUG_RX literal not found in web/voice.html"
    assert m.group("pat") == SLUG_RE.pattern, (
        f"client regex {m.group('pat')!r} drifted from server {SLUG_RE.pattern!r}"
    )
