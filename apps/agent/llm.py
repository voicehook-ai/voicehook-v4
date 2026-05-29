"""LLM factory — Gemini 2.5 Flash.

Single provider, no fallback chain (every fallback path is a silent failure
mode — see voicehook-v3 demo-agent post-mortem, PLAN-v4.md inventory).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.plugins.google import LLM as GoogleLLM

DEFAULT_MODEL = "gemini-2.5-flash"


def build_llm(*, model: str | None = None) -> GoogleLLM:
    """Gemini 2.5 Flash. Requires GOOGLE_API_KEY in the env."""
    from livekit.plugins.google import LLM

    return LLM(model=model or os.environ.get("VOICEHOOK_LLM_MODEL", DEFAULT_MODEL))
