"""operator.* data-channel relay — knowledge-transfer mouthpiece.

Design (mode B, voicehook-v3#28 + Wissenstransfer):
- Default: the voice-ai MAY answer simple questions from its persona (the
  pushed knowledge-graph) — RelayAgent does NOT stop the LLM. For anything
  substantive/unknown the persona instructs it to defer to the operator, who
  pushes the verbatim answer via `operator.say`.
- Strict mode: `operator.mode`={"mode":"strict"} (the CLI's --strict-relay)
  flips RelayAgent.strict → on_user_turn_completed raises StopResponse, so the
  LLM never produces a turn on its own (pure mouthpiece).

Topics handled here:
- operator.say        — TTS the text. Default mode "replace": storniert alles noch
                        Ungesprochene + die laufende Ausgabe; mode "append" hängt an
- operator.persona    — replace the agent's instructions (live-injected knowledge)
- operator.mode       — switch strict/auto generation ({"mode":"strict"|"auto"})
- operator.interrupt  — drop the current say AND everything queued behind it
- operator.inject     — synthetic user-turn (test harness; operator reads transcript)

PR-12 adds: every operator.say also publishes {role:"agent",text:...} on the
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

TOPIC_SAY = "operator.say"
TOPIC_PERSONA = "operator.persona"
TOPIC_MODE = "operator.mode"
TOPIC_INTERRUPT = "operator.interrupt"
TOPIC_INJECT = "operator.inject"

DEFAULT_PERSONA = (
    "Du bist die Stimme von voicehook.ai. Du antwortest aus deinem Kontext "
    "(was dir der Operator als Persona/Graph gegeben hat). Simple Fragen "
    "beantwortest du selbst, kurz und praezise. Fuer alles Substantielle, "
    "Technische oder Unbekannte sagst du 'Moment, ich geb das an den Operator' "
    "und wartest auf operator.say. Du erfindest NICHTS."
)


class RelayAgent(Agent):
    """Mouthpiece with two modes:
      - auto (default): the LLM answers simple questions from its persona
        (the pushed knowledge-graph); operator.say overrides substantive content.
      - strict: StopResponse on every turn — the LLM never speaks on its own."""

    def __init__(self, *, instructions: str = "", strict: bool = False, **kwargs) -> None:  # noqa: ANN003
        super().__init__(instructions=instructions, **kwargs)
        self.strict = strict

    async def on_user_turn_completed(self, *args, **kwargs) -> None:  # noqa: D401, ANN001
        if self.strict:
            raise StopResponse()
        # auto mode: fall through → the LLM answers from its persona (knowledge transfer).


@dataclass
class RelayHandlers:
    """Resolved handlers for unit-test introspection."""

    on_say: callable
    on_persona: callable
    on_mode: callable
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


def _is_done(handle: object) -> bool:
    try:
        return bool(handle.done())
    except Exception:  # noqa: BLE001
        return False


def build_relay_handlers(
    session: AgentSession,
    agent: RelayAgent,
    *,
    room: Room | None = None,
) -> RelayHandlers:
    """Build per-topic handler closures bound to a session + agent.

    `room` is optional — when passed, every operator.say also publishes a
    {role:"agent",text:...} packet on the `transcript` topic so the browser UI
    can render the agent turn. (v3 parity, PR-12.)
    """

    # Noch nicht fertig gesprochene operator.say-Ausgaben. session.say() hängt
    # nur hinten an die Queue an; ohne Buchführung spricht der Agent Minuten
    # später Aussagen, die der Operator längst revidiert hat.
    pending: list = []

    def _cancel_all() -> int:
        """Storniert alles Ungesprochene + die laufende Ausgabe (auch Eigenantworten).

        force=True ist nötig: livekit wirft sonst RuntimeError statt zu stoppen,
        sobald die laufende Ausgabe keine Unterbrechung erlaubt.
        """
        dropped = 0
        for handle in pending:
            try:
                if not handle.done():
                    handle.interrupt(force=True)
                    dropped += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("[operator.say] cancel handle: %s", e)
        pending.clear()
        try:
            session.interrupt(force=True)
        except Exception as e:  # noqa: BLE001 — nichts läuft / Session gestoppt
            logger.debug("[operator.say] session.interrupt: %s", e)
        return dropped

    async def on_say(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        # Default "replace": die neue Aussage ersetzt alles noch Ungesprochene.
        # "append" nur für bewusst mehrteilige Ausgaben (Teil 1, Teil 2, ...).
        mode = (data.get("mode") or "replace").strip().lower()
        if mode != "append" or data.get("priority") == "interrupt":
            dropped = _cancel_all()
            if dropped:
                logger.info("[operator.say] %d veraltete Ausgabe(n) storniert", dropped)
        logger.info("[operator.say] %s", text[:200])
        _publish_transcript_safe(room, "agent", text)
        # allow_interruptions=True = full-duplex barge-in: the user can comment
        # while the mouthpiece is speaking and the STT keeps hearing them.
        handle = session.say(text, allow_interruptions=True)
        pending[:] = [h for h in pending if not _is_done(h)]
        if handle is not None:
            pending.append(handle)

    async def on_persona(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        await agent.update_instructions(text)
        logger.info("[operator.persona] %d chars injected", len(text))

    async def on_mode(packet: DataPacket) -> None:
        data = _decode(packet.data)
        mode = (data.get("mode") or "").strip().lower()
        agent.strict = mode == "strict"
        logger.info("[operator.mode] strict=%s", agent.strict)

    async def on_interrupt(_packet: DataPacket) -> None:
        logger.info("[operator.interrupt] %d Ausgabe(n) storniert", _cancel_all())

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
        logger.info("[operator.inject] %s", text[:200])

    return RelayHandlers(
        on_say=on_say, on_persona=on_persona, on_mode=on_mode,
        on_interrupt=on_interrupt, on_inject=on_inject,
    )


def topic_dispatch(handlers: RelayHandlers) -> dict[str, callable]:
    """Map operator.* topic → handler. Used by the worker's data-channel subscription."""
    return {
        TOPIC_SAY: handlers.on_say,
        TOPIC_PERSONA: handlers.on_persona,
        TOPIC_MODE: handlers.on_mode,
        TOPIC_INTERRUPT: handlers.on_interrupt,
        TOPIC_INJECT: handlers.on_inject,
    }
