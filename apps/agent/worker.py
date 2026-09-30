"""LiveKit worker — joins dispatched rooms as `voice-ai`.

PR-5 wires the full mouthpiece: connect → start AgentSession(stt+tts+llm) →
bind operator.* data-channel handlers → RelayAgent.on_user_turn_completed
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


def is_live() -> bool:
    """Gemini-Live-Testworker (eigener Dienst voice-ai-live), sonst klassisch."""
    return os.environ.get("VOICEHOOK_PIPELINE", "pipeline").strip().lower() == "live"


def build_session() -> AgentSession:
    if is_live():
        from . import live

        return AgentSession(llm=live.build_live_llm())
    return AgentSession(stt=build_stt(), tts=build_tts(), llm=build_llm())


async def entrypoint(ctx: JobContext) -> None:
    """Connect, start the mouthpiece session, bind operator.* handlers."""
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

    live_mode = is_live()
    session = build_session()
    agent = RelayAgent(instructions=DEFAULT_PERSONA)
    handlers = build_relay_handlers(session, agent, room=ctx.room, live=live_mode)
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

    if live_mode:
        from .live import live_cost_usd

        totals = {"usd": 0.0, "turns": 0}

        # Kosten je Turn messen (Live API rechnet den ganzen Kontext pro Turn ab)
        @session.on("metrics_collected")
        def _on_metrics(ev) -> None:  # noqa: ANN001
            m = getattr(ev, "metrics", None)
            if type(m).__name__ != "RealtimeModelMetrics":
                return
            usd = live_cost_usd(m)
            totals["usd"] += usd
            totals["turns"] += 1
            logger.info(
                "[live-cost] room=%s turn=%d in=%d out=%d usd=%.5f total_usd=%.4f",
                ctx.room.name, totals["turns"], getattr(m, "input_tokens", 0),
                getattr(m, "output_tokens", 0), usd, totals["usd"],
            )

        # Was Gemini tatsächlich gesagt hat -> Browser-Transkript (+ Operator liest mit)
        @session.on("conversation_item_added")
        def _on_item(ev) -> None:  # noqa: ANN001
            item = getattr(ev, "item", None)
            if getattr(item, "role", None) != "assistant":
                return
            text = getattr(item, "text_content", "") or ""
            if not text:
                return
            payload = json.dumps({"role": "agent", "text": text}).encode()

            async def _send() -> None:
                try:
                    await ctx.room.local_participant.publish_data(payload=payload, topic="transcript")
                except Exception as e:  # noqa: BLE001
                    logger.warning("[agent-transcript publish] %s", e)
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
