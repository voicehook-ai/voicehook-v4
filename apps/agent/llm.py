"""LLM factory — Gemini 2.5 Flash.

Single provider, no fallback chain (every fallback path is a silent failure
mode — see voicehook-v3 demo-agent post-mortem, PLAN-v4.md inventory).

Denk-Budget (Oliver 02.10.2026): gemini-2.5-flash denkt ohne thinking_config bei
jeder Antwort mit. Gemessen 02.10. mit echtem Call (Delta-Prompt, 3 typische Fragen):
ohne Konfiguration 247 bis 326 Denk-Tokens und 1,95 s Median, mit thinking_budget=0
0 Denk-Tokens und 0,57 s. Default war deshalb 0.
Seit fix/delta-core-rule3 (02.10.): Default 128. -1 (dynamisch) dachte 880 bis 1380 Tokens,
4,4 bis 6,7 s; im Prod-Call brach das nächste Nutzer-Fragment jede Generierung ab, Delta
blieb stumm. Repro (je 100 Läufe, Fix): 0 = 0,48 s Median, aber 14/30 nur "Moment" bei
vollem Status; 128 = 0,98 s, 1/30, Status 6/6 vollständig vorgelesen; 256 = 1,32 s, 0/30.
Per VOICEHOOK_LLM_THINKING_BUDGET überschreibbar (0 = aus, -1 = dynamisch, leer/"off" =
Modell-Default). Der Verlaufs-Zusammenfasser (history.py) denkt weiter nicht.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from livekit.plugins.google import LLM as GoogleLLM

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_THINKING_BUDGET = 128  # kleinstes Budget mit sauberer Regeltreue


def thinking_budget() -> int | None:
    """VOICEHOOK_LLM_THINKING_BUDGET: Zahl -> Budget, leer/"off" -> None (Modell-Default)."""
    raw = os.environ.get("VOICEHOOK_LLM_THINKING_BUDGET")
    if raw is None:
        return DEFAULT_THINKING_BUDGET
    raw = raw.strip().lower()
    if raw in ("", "off", "default", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_THINKING_BUDGET


def thinking_kwargs() -> dict:
    budget = thinking_budget()
    return {} if budget is None else {"thinking_config": {"thinking_budget": budget}}


def build_llm(*, model: str | None = None) -> GoogleLLM:
    """Gemini 2.5 Flash. Requires GOOGLE_API_KEY in the env."""
    from livekit.plugins.google import LLM

    return LLM(model=model or os.environ.get("VOICEHOOK_LLM_MODEL", DEFAULT_MODEL),
               **thinking_kwargs())
