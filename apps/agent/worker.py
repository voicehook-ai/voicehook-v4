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

from livekit.agents import AgentSession, JobContext, WorkerOptions, cli

from .llm import build_llm
from .relay import DEFAULT_PERSONA, RelayAgent, build_relay_handlers, topic_dispatch
from .voice import build_stt, build_tts

logger = logging.getLogger("voicehook.worker")

AGENT_NAME = os.environ.get("VOICEHOOK_AGENT_NAME", "voice-ai")


async def entrypoint(ctx: JobContext) -> None:
    """Connect, start the mouthpiece session, bind senior.* handlers."""
    await ctx.connect()
    logger.info(
        "agent joined room=%s job=%s identity=%s",
        ctx.room.name,
        ctx.job.id,
        ctx.room.local_participant.identity,
    )

    session = AgentSession(stt=build_stt(), tts=build_tts(), llm=build_llm())
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    handlers = build_relay_handlers(session, agent)
    routes = topic_dispatch(handlers)

    @ctx.room.on("data_received")
    def _on_data(packet) -> None:  # noqa: ANN001
        handler = routes.get(packet.topic)
        if handler is None:
            return
        import asyncio
        asyncio.create_task(handler(packet))

    await session.start(agent=agent, room=ctx.room)


def build_worker_options() -> WorkerOptions:
    """Factory exposed for unit tests."""
    return WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
