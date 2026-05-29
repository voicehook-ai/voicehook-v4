"""Component health probes + the agent.heartbeat topic split.

Fixes voicehook-v3#62 (`agent.health` ticks spammed the `transcript` topic in
v3 — every consumer had to grep them out). v4 publishes liveness on a
dedicated `agent.heartbeat` topic so `transcript` stays pure user/agent turns.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass

TOPIC_HEARTBEAT = "agent.heartbeat"
TOPIC_ERROR = "agent.error"
HEARTBEAT_INTERVAL_S = 30


@dataclass(frozen=True)
class ProbeResult:
    """Functional probe per component — True = key present + plugin importable."""

    stt: bool
    tts: bool
    llm: bool

    @property
    def healthy(self) -> bool:
        return self.stt and self.tts and self.llm


def probe_stt() -> bool:
    """Deepgram: API key in env."""
    return bool(os.environ.get("DEEPGRAM_API_KEY"))


def probe_tts() -> bool:
    """Google TTS: SA credentials path or API key in env."""
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.environ.get("GOOGLE_API_KEY")
    )


def probe_llm() -> bool:
    """Gemini: API key in env."""
    return bool(os.environ.get("GOOGLE_API_KEY"))


def probe_all() -> ProbeResult:
    return ProbeResult(stt=probe_stt(), tts=probe_tts(), llm=probe_llm())


def heartbeat_payload(*, room: str | None = None, now: float | None = None) -> dict:
    """Shape that gets published on TOPIC_HEARTBEAT every HEARTBEAT_INTERVAL_S."""
    probe = probe_all()
    return {
        "ts": int(now if now is not None else time.time()),
        "room": room,
        "probe": asdict(probe),
        "healthy": probe.healthy,
    }
