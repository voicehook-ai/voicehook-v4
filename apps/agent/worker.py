"""LiveKit worker — joins dispatched rooms as `voice-ai`.

PR-3 ships the worker skeleton: registers with the LiveKit server, accepts
dispatches, connects to the room, logs the join. STT/TTS/LLM components land
in PR-4/5; senior.* data-channel relay in PR-5; health/status in PR-6.

Run locally:
    python -m agent.worker dev   # connects to LIVEKIT_URL with API creds
"""

from __future__ import annotations

import logging
import os

from livekit.agents import JobContext, WorkerOptions, cli

logger = logging.getLogger("voicehook.worker")

AGENT_NAME = os.environ.get("VOICEHOOK_AGENT_NAME", "voice-ai")


async def entrypoint(ctx: JobContext) -> None:
    """Connect to the dispatched room and idle. Real session wires up PR-4+."""
    await ctx.connect()
    logger.info(
        "agent joined room=%s job=%s identity=%s",
        ctx.room.name,
        ctx.job.id,
        ctx.room.local_participant.identity,
    )
    # PR-4 will spin up AgentSession with STT + TTS + LLM here.
    # For now: just hold the connection until the participant disconnects.


def build_worker_options() -> WorkerOptions:
    """Factory exposed for unit tests."""
    return WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
