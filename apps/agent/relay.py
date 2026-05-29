"""senior.* data-channel relay — the agent is a STRICT MOUTHPIECE.

Design (from PLAN-v4.md + voicehook-v3#28):
- The senior brain (a Claude/external agent) pushes content over the LiveKit
  data channel. The voice-ai may speak ONLY:
    1) what `senior.say` delivers (verbatim TTS),
    2) what is in the injected `senior.persona` (bounded knowledge),
    3) verbatim web-search quotes (single self-tool, not in PR-5).
- The agent NEVER auto-generates a reply. RelayAgent.on_user_turn_completed
  raises StopResponse so the LLM never produces a turn on its own.

Topics handled here:
- senior.say        — TTS the text immediately (priority=interrupt drops the floor)
- senior.persona    — replace the agent's instructions (live-injected knowledge)
- senior.interrupt  — drop the current say
- senior.inject     — synthetic user-turn (test harness; senior brain reads transcript)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from livekit.agents import Agent, StopResponse

if TYPE_CHECKING:
    from livekit.agents import AgentSession
    from livekit.rtc import DataPacket

logger = logging.getLogger("voicehook.relay")

TOPIC_SAY = "senior.say"
TOPIC_PERSONA = "senior.persona"
TOPIC_INTERRUPT = "senior.interrupt"
TOPIC_INJECT = "senior.inject"

DEFAULT_PERSONA = (
    "Du bist die Stimme von voicehook.ai. Du sagst NUR was dir explizit "
    "via senior.say geschickt wird oder was in deiner persona steht. "
    "Du erfindest NICHTS. Wenn nicht im Wissen: 'Moment, frag ich Claude'."
)


class RelayAgent(Agent):
    """Mouthpiece agent: never generates its own reply, only relays senior.*."""

    async def on_user_turn_completed(self, *args, **kwargs) -> None:  # noqa: D401, ANN001
        # NEVER auto-respond — the senior brain decides what to say.
        raise StopResponse()


@dataclass
class RelayHandlers:
    """Resolved handlers for unit-test introspection."""

    on_say: callable
    on_persona: callable
    on_interrupt: callable
    on_inject: callable


def _decode(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


def build_relay_handlers(session: AgentSession, agent: RelayAgent) -> RelayHandlers:
    """Build per-topic handler closures bound to a session + agent.

    Exposed for unit tests; the worker wires these via ctx.room.on('data_received').
    """

    async def on_say(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        priority = data.get("priority")
        if priority == "interrupt":
            session.interrupt()
        logger.info("[senior.say] %s", text[:200])
        session.say(text, allow_interruptions=False)

    async def on_persona(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        await agent.update_instructions(text)
        logger.info("[senior.persona] %d chars injected", len(text))

    async def on_interrupt(_packet: DataPacket) -> None:
        logger.info("[senior.interrupt]")
        session.interrupt()

    async def on_inject(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        # Synthetic turn into the chat-context (NOT spoken). The agent ctx is
        # read-only, so copy → mutate → update_chat_ctx.
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role=data.get("role", "user"), content=text)
        await agent.update_chat_ctx(ctx)
        logger.info("[senior.inject] %s", text[:200])

    return RelayHandlers(on_say=on_say, on_persona=on_persona, on_interrupt=on_interrupt, on_inject=on_inject)


def topic_dispatch(handlers: RelayHandlers) -> dict[str, callable]:
    """Map senior.* topic → handler. Used by the worker's data-channel subscription."""
    return {
        TOPIC_SAY: handlers.on_say,
        TOPIC_PERSONA: handlers.on_persona,
        TOPIC_INTERRUPT: handlers.on_interrupt,
        TOPIC_INJECT: handlers.on_inject,
    }
