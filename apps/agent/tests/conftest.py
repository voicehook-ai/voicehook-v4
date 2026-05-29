"""Shared pytest setup — fake API keys so factories construct in CI without real creds."""

from __future__ import annotations

import os

# Apply once at import-time (before any test module imports a livekit plugin).
os.environ.setdefault("DEEPGRAM_API_KEY", "test-deepgram-key-do-not-use")
os.environ.setdefault("GOOGLE_API_KEY", "test-google-key-do-not-use")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")
