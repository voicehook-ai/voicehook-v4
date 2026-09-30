"""LiveKit worker — joins dispatched rooms as `voice-ai`.

PR-5 wires the full mouthpiece: connect → start AgentSession(stt+tts+llm) →
bind operator.* data-channel handlers → RelayAgent.on_user_turn_completed
raises StopResponse so the model never produces a turn on its own.

Run locally:
    python -m agent.worker dev   # connects to LIVEKIT_URL with API creds
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession, AutoSubscribe, JobContext, WorkerOptions, cli, room_io

from . import budget
from .llm import build_llm
from .relay import DEFAULT_PERSONA, RelayAgent, build_relay_handlers, topic_dispatch
from .voice import build_stt, build_tts

logger = logging.getLogger("voicehook.worker")

AGENT_NAME = os.environ.get("VOICEHOOK_AGENT_NAME", "voice-ai")

# ---------------------------------------------------------------------------
# Cost caps (Oliver, absolute): no paid call (STT/TTS/LLM) may run unbounded,
# test calls included. Both caps are ALWAYS on — invalid / <=0 env values fall
# back to the default instead of disabling the cap.
# ---------------------------------------------------------------------------
DEFAULT_MAX_CALL_SECONDS = 3600.0
DEFAULT_IDLE_NO_HUMAN_SECONDS = 60.0
MAX_CALL_ANNOUNCEMENT = "Maximale Gesprächsdauer erreicht. Ich beende den Call."
LIVE_BUDGET_ANNOUNCEMENT = "Das Live-Budget für diesen Monat ist aufgebraucht. Ich beende den Call."
# Every teardown step is bounded so the teardown itself can never hang.
TEARDOWN_STEP_TIMEOUT = 10.0

# Non-human identities — mirrors vhPresenceSlotFor() in web/voice.html:
# voice-ai worker (`voice-ai`, `agent-AJ_*`) and senior CLIs (claude-*, hermes-*, …).
_NON_HUMAN_IDENTITY = re.compile(
    r"^(voice-ai|agent-AJ_)"
    r"|^(claude|hermes|cursor|openclaw|zeroclaw|codex|gemini|gpt|grok|llama|qwen|deepseek|senior|agent-cli)-",
    re.IGNORECASE,
)


def _positive_env_seconds(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def max_call_seconds() -> float:
    return _positive_env_seconds("VH_MAX_CALL_SECONDS", DEFAULT_MAX_CALL_SECONDS)


def idle_no_human_seconds() -> float:
    return _positive_env_seconds("VH_IDLE_NO_HUMAN_SECONDS", DEFAULT_IDLE_NO_HUMAN_SECONDS)


def is_human(participant: Any) -> bool:
    """True for a remote participant that is (probably) a person.

    Agent-kind participants and known bot identities are not human. Unknown
    identities count as human (never cut a real person); the hard max-duration
    cap is the backstop for those.
    """
    if getattr(participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
        return False
    return not _NON_HUMAN_IDENTITY.search(getattr(participant, "identity", "") or "")


class CallGuard:
    """Enforces the per-call cost caps for one voice-ai job.

    - hard cap: after `max_seconds` announce briefly, end the session, delete the room.
    - idle cap: no human in the room for `idle_seconds` → end session, delete the room.
    - any other session close (e.g. linked participant left) → leave the room
      (and delete it when no human is left), so voice-ai never lingers.
    Every end logs exactly one structured `call_end` line (reason + duration).
    """

    def __init__(
        self,
        ctx: Any,
        session: Any,
        *,
        max_seconds: float,
        idle_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        live: bool = False,
    ) -> None:
        self._live = live
        self._ctx = ctx
        self._session = session
        self._max_seconds = max_seconds
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._started_at = clock()
        self._ended = False
        self._max_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._end_task: asyncio.Task | None = None
        self.end_reason: str | None = None

    # -- wiring -------------------------------------------------------------
    def start(self) -> None:
        room = self._ctx.room
        room.on("participant_connected", self._on_participant_connected)
        room.on("participant_disconnected", self._on_participant_disconnected)
        self._session.on("close", self._on_session_close)
        self._max_task = asyncio.create_task(self._max_duration_timer())
        self._reevaluate_idle()

    def human_count(self) -> int:
        return sum(1 for p in self._ctx.room.remote_participants.values() if is_human(p))

    # -- events -------------------------------------------------------------
    def _on_participant_connected(self, participant: Any) -> None:
        self._reevaluate_idle()

    def _on_participant_disconnected(self, participant: Any) -> None:
        self._reevaluate_idle()

    def _on_session_close(self, ev: Any) -> None:
        reason = getattr(getattr(ev, "reason", None), "value", None) or str(
            getattr(ev, "reason", "unknown")
        )
        self._schedule_end(f"session_close:{reason}", delete_room=False)

    # -- timers -------------------------------------------------------------
    def _reevaluate_idle(self) -> None:
        if self._ended:
            return
        if self.human_count() > 0:
            if self._idle_task is not None:
                self._idle_task.cancel()
                self._idle_task = None
        elif self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_timer())

    async def _idle_timer(self) -> None:
        await asyncio.sleep(self._idle_seconds)
        if self.human_count() == 0:
            # Nur der Agent geht (Kosten stoppen); der Raum bleibt, damit ein wartender
            # Operator nicht rausfliegt. Kommt ein Mensch zurück, dispatcht der Server neu.
            await self.end("idle_no_human", delete_room=False)
        else:
            self._idle_task = None

    async def _max_duration_timer(self) -> None:
        await asyncio.sleep(self._max_seconds)
        await self.end("max_duration", delete_room=True, announce=MAX_CALL_ANNOUNCEMENT)

    # -- teardown -----------------------------------------------------------
    def _schedule_end(self, reason: str, *, delete_room: bool) -> None:
        if self._ended or self._end_task is not None:
            return
        self._end_task = asyncio.create_task(self.end(reason, delete_room=delete_room))

    async def end(self, reason: str, *, delete_room: bool, announce: str | None = None) -> None:
        """Idempotent, bounded teardown: announce → close session → delete room → leave job."""
        if self._ended:
            return
        self._ended = True
        self.end_reason = reason
        current = asyncio.current_task()
        for task in (self._max_task, self._idle_task):
            if task is not None and task is not current:
                task.cancel()
        duration = self._clock() - self._started_at
        room_name = getattr(self._ctx.room, "name", "")
        logger.info(
            "call_end %s",
            json.dumps(
                {
                    "event": "call_end",
                    "reason": reason,
                    "duration_s": round(duration, 1),
                    "room": room_name,
                    "job": getattr(getattr(self._ctx, "job", None), "id", ""),
                    "humans": self.human_count(),
                    "delete_room": delete_room,
                    "max_s": self._max_seconds,
                    "idle_s": self._idle_seconds,
                }
            ),
        )
        try:
            if announce:
                await self._bounded("announce", self._announce(announce))
            if not reason.startswith("session_close:"):
                await self._bounded("session_close", self._session.aclose())
            if delete_room:
                await self._bounded("delete_room", self._ctx.delete_room(room_name))
        finally:
            self._ctx.shutdown(reason=f"call_guard:{reason}")

    async def _announce(self, text: str) -> None:
        if self._live:  # Realtime-Modell hat kein TTS für say()
            handle = self._session.generate_reply(
                instructions=f"Sag jetzt wörtlich und nur das: {text}", allow_interruptions=False
            )
        else:
            handle = self._session.say(text, allow_interruptions=False)
        await handle.wait_for_playout()

    async def _bounded(self, step: str, aw: Any) -> None:
        try:
            await asyncio.wait_for(aw, timeout=TEARDOWN_STEP_TIMEOUT)
        except Exception as e:  # noqa: BLE001 — teardown must always reach shutdown
            logger.warning("[call_guard] %s failed: %r", step, e)


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
    if live_mode and budget.exhausted():
        # Monatsbudget weg: gar nicht erst eine kostenpflichtige Live-Session öffnen.
        logger.warning("[live-budget] exhausted (%.2f USD), refusing room=%s", budget.spent_usd(), ctx.room.name)
        ctx.shutdown(reason="live_budget_exhausted")
        return
    session = build_session()
    if live_mode:
        from .live import LIVE_BASE_INSTRUCTIONS

        agent = RelayAgent(instructions=LIVE_BASE_INSTRUCTIONS)
    else:
        from .gate import SpeechGate, gate_enabled, load_vad

        gate = SpeechGate(load_vad()) if gate_enabled() else None
        agent = RelayAgent(instructions=DEFAULT_PERSONA, gate=gate)
    handlers = build_relay_handlers(session, agent, room=ctx.room, live=live_mode)
    routes = topic_dispatch(handlers)

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

    # Laufende Kosten (beide Modi) -> Log + Topic `cost` für die Anzeige im Browser
    from .live import metric_cost_usd

    totals = {"usd": 0.0, "turns": 0, "sent_at": 0.0}
    guard_ref: list[CallGuard] = []  # wird unten gesetzt; Budget-Ende braucht den Guard

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:  # noqa: ANN001
        m = getattr(ev, "metrics", None)
        usd = metric_cost_usd(m)
        if usd <= 0:
            return
        totals["usd"] += usd
        if live_mode:
            month = budget.add_usd(usd)
            if month >= budget.limit_usd() and guard_ref:
                logger.warning("[live-budget] reached %.4f USD in room=%s, ending call", month, ctx.room.name)
                asyncio.create_task(
                    guard_ref[0].end("live_budget", delete_room=False, announce=LIVE_BUDGET_ANNOUNCEMENT)
                )
        if type(m).__name__ == "RealtimeModelMetrics":
            totals["turns"] += 1
            logger.info(
                "[live-cost] room=%s turn=%d in=%d out=%d usd=%.5f total_usd=%.4f",
                ctx.room.name, totals["turns"], getattr(m, "input_tokens", 0),
                getattr(m, "output_tokens", 0), usd, totals["usd"],
            )
        now = time.monotonic()
        if now - totals["sent_at"] < 2.0:  # höchstens alle 2 s an den Browser
            return
        totals["sent_at"] = now
        payload = json.dumps({"usd": round(totals["usd"], 5), "mode": "live" if live_mode else "pipeline"}).encode()

        async def _send_cost() -> None:
            try:
                await ctx.room.local_participant.publish_data(payload=payload, topic="cost")
            except Exception as e:  # noqa: BLE001
                logger.debug("[cost publish] %s", e)
        asyncio.create_task(_send_cost())

    # Alles tatsächlich Gesprochene (Eigenantworten + Operator-Sätze, beide Modi)
    # -> Browser-Transkript; bei Abbruch nur der gesprochene Teil (synchronized transcript)
    @session.on("conversation_item_added")
    def _on_item(ev) -> None:  # noqa: ANN001
        item = getattr(ev, "item", None)
        if getattr(item, "role", None) != "assistant":
            return
        text = (getattr(item, "text_content", "") or "").strip()
        from .live import NO_SPEECH_MARKERS

        if not text or text in NO_SPEECH_MARKERS:
            return
        role = "operator" if handlers.is_operator_speech(session.current_speech, text) else "agent"
        payload = json.dumps({"role": role, "text": text}).encode()

        async def _send() -> None:
            try:
                await ctx.room.local_participant.publish_data(payload=payload, topic="transcript")
            except Exception as e:  # noqa: BLE001
                logger.warning("[agent-transcript publish] %s", e)
        asyncio.create_task(_send())

    # Cost caps armed BEFORE session.start so even a hanging start is bounded.
    guard = CallGuard(
        ctx,
        session,
        max_seconds=max_call_seconds(),
        idle_seconds=idle_no_human_seconds(),
        live=live_mode,
    )
    guard_ref.append(guard)
    guard.start()
    # Explicit room options (don't rely on lib defaults): close the session when
    # the linked participant leaves. Room deletion is owned by CallGuard so it
    # only happens when no human is left (or on the hard cap).
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(close_on_disconnect=True, delete_room_on_close=False),
    )


def build_worker_options() -> WorkerOptions:
    """Factory exposed for unit tests."""
    return WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cli.run_app(build_worker_options())


if __name__ == "__main__":
    main()
