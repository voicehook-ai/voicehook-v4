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

PR-12 adds: every senior.say also publishes {role:"agent",text:...} on the
`transcript` topic so the browser UI can render it (v3 parity).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from livekit.agents import Agent, StopResponse

if TYPE_CHECKING:
    from livekit.agents import AgentSession
    from livekit.rtc import DataPacket, Room

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


TOPIC_TRANSCRIPT = "transcript"


def _publish_transcript_safe(room: Room | None, role: str, text: str) -> None:
    """Fire-and-forget publish_data; never let UI feedback break the relay."""
    if room is None or not text:
        return
    payload = json.dumps({"role": role, "text": text}).encode()
    async def _send() -> None:
        try:
            await room.local_participant.publish_data(payload=payload, topic=TOPIC_TRANSCRIPT)
        except Exception as e:  # noqa: BLE001
            logger.warning("[transcript publish] %s", e)
    asyncio.create_task(_send())


def build_relay_handlers(
    session: AgentSession,
    agent: RelayAgent,
    *,
    room: Room | None = None,
) -> RelayHandlers:
    """Build per-topic handler closures bound to a session + agent.

    `room` is optional — when passed, every senior.say also publishes a
    {role:"agent",text:...} packet on the `transcript` topic so the browser UI
    can render the agent turn. (v3 parity, PR-12.)
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
        _publish_transcript_safe(room, "agent", text)
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
