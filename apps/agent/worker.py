"""LiveKit worker — joins dispatched rooms as `voice-ai`.

PR-5 wires the full mouthpiece: connect → start AgentSession(stt+tts+llm) →
bind senior.* data-channel handlers → RelayAgent.on_user_turn_completed
raises StopResponse so the model never produces a turn on its own.

Run locally:
    python -m agent.worker dev   # connects to LIVEKIT_URL with API creds
"""

from __future__ import annotations

import logging
import os

from livekit.agents import AgentSession, AutoSubscribe, JobContext, WorkerOptions, cli

from .llm import build_llm
from .relay import DEFAULT_PERSONA, RelayAgent, build_relay_handlers, topic_dispatch
from .voice import build_stt, build_tts

logger = logging.getLogger("voicehook.worker")

AGENT_NAME = os.environ.get("VOICEHOOK_AGENT_NAME", "voice-ai")


async def entrypoint(ctx: JobContext) -> None:
    """Connect, start the mouthpiece session, bind senior.* handlers."""
    # Explicitly subscribe to participant audio so STT always has an input track.
    # Plain ctx.connect() leaves auto-subscribe to the lib default, which has
    # bitten us repeatedly: VAD ("speaking") still fires server-side but the
    # agent never receives the audio track → STT produces zero transcripts
    # (#55). AUDIO_ONLY is what an STT mouthpiece actually needs.
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    logger.info(
        "agent joined room=%s job=%s identity=%s",
        ctx.room.name,
        ctx.job.id,
        ctx.room.local_participant.identity,
    )

    session = AgentSession(stt=build_stt(), tts=build_tts(), llm=build_llm())
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    handlers = build_relay_handlers(session, agent, room=ctx.room)
    routes = topic_dispatch(handlers)

    import asyncio
    import json

    @ctx.room.on("data_received")
    def _on_data(packet) -> None:  # noqa: ANN001
        handler = routes.get(packet.topic)
        if handler is None:
            return
        asyncio.create_task(handler(packet))

    # Publish user STT transcripts back on the `transcript` topic so the
    # browser UI sees what the agent heard. (v3 parity, PR-12.)
    @session.on("user_input_transcribed")
    def _on_user_transcript(ev) -> None:  # noqa: ANN001
        text = getattr(ev, "transcript", None) or getattr(ev, "text", "")
        is_final = getattr(ev, "is_final", True)
        if not text or not is_final:
            return
        payload = json.dumps({"role": "user", "text": text}).encode()
        async def _send() -> None:
            try:
                await ctx.room.local_participant.publish_data(payload=payload, topic="transcript")
            except Exception as e:  # noqa: BLE001
                logger.warning("[user-transcript publish] %s", e)
        asyncio.create_task(_send())

    await session.start(agent=agent, room=ctx.room)


def build_worker_options() -> WorkerOptions:
    """Factory exposed for unit tests."""
    return WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
