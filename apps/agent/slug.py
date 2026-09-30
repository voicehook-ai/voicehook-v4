"""Canonical room-slug format — single source of truth.

The client (web/voice.html) only auto-joins a /r/<slug> deeplink whose slug
matches VH_SLUG_RX = /^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}$/ (three lowercase
words + a 4-8 char upper/digit suffix). Anything else → VH_PREFILL_ROOM is null
→ autostart silently no-ops → "the room never connects".

That exact bug bit us 2026-05-29: invites were minted for slugs like `nvu-GKJM`
(one word) which the client rejected, so auto-join never fired and looked like a
broken call. The slug regex lived only in the client and was duplicated, drifted,
ad-hoc in the server's host-call path. This module is now the ONE place both the
server and the invite CLI agree with the client.
"""

from __future__ import annotations

import re
import secrets

# Must stay byte-for-byte equivalent to VH_SLUG_RX in web/voice.html.
SLUG_RE = re.compile(r"^[a-z]+-[a-z]+-[a-z]+-[A-Z0-9]{4,8}$")

# Suffix avoids look-alike chars (no I/O/0/1) so a spoken/typed slug is unambiguous.
SUFFIX_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

WORDS = [
    "fresh", "signal", "clear", "drift", "bright", "swift", "calm", "bold",
    "lucid", "prime", "spark", "vivid", "quiet", "rapid", "solid", "keen",
    "brisk", "lunar", "solar", "amber", "ivory", "cobalt", "onyx", "ember",
    "orbit", "nova", "quartz", "tide", "pulse", "vector", "lumen", "flux",
]


def is_valid_slug(slug: str) -> bool:
    """True iff the client would accept this slug as an auto-join deeplink."""
    return bool(SLUG_RE.match(slug))


def gen_slug() -> str:
    """3 lowercase words + 4-char suffix — guaranteed to match SLUG_RE."""
    words = "-".join(secrets.choice(WORDS) for _ in range(3))
    suffix = "".join(secrets.choice(SUFFIX_ALPHABET) for _ in range(4))
    return f"{words}-{suffix}"
