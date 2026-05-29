"""Server-side auto-greet, heartbeat, backchannel — kill senior-roundtrip lag.

voicehook-v3#61: the senior brain previously had to push a greet right after
`persona auto-pushed`, an 8s heartbeat during work, and a 3-word backchannel
when the user spoke >6s. Round-trip CLI lag ate the window. v4 fires these
server-side; senior brain only pushes substantive replies.

- AutoGreeter         — fires senior.say once when first persona arrives
- HeartbeatPublisher  — emits heartbeat_payload() on TOPIC_HEARTBEAT every 30s
- BackchannelWatcher  — when user has been speaking >6s and the agent hasn't
                        spoken, auto-fire one of ["mhm","ja","ok"] (cycling)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .health import HEARTBEAT_INTERVAL_S, TOPIC_HEARTBEAT, heartbeat_payload

if TYPE_CHECKING:
    from livekit.agents import AgentSession
    from livekit.rtc import Room

logger = logging.getLogger("voicehook.signals")

DEFAULT_GREET = "Hallo, hier ist voicehook. Was brauchst du?"
BACKCHANNEL_CYCLE = ["mhm", "ja", "ok"]
BACKCHANNEL_AFTER_SECONDS = 6.0


class AutoGreeter:
    """Fires session.say(greet) exactly once on first persona push."""

    def __init__(self, session: AgentSession, greet: str = DEFAULT_GREET) -> None:
        self._session = session
        self._greet = greet
        self._fired = False

    def on_persona(self) -> None:
        if self._fired:
            return
        self._fired = True
        logger.info("[auto-greet] %s", self._greet)
        self._session.say(self._greet, allow_interruptions=False)

    @property
    def fired(self) -> bool:
        return self._fired


class HeartbeatPublisher:
    """Publishes heartbeat_payload on TOPIC_HEARTBEAT every interval_s."""

    def __init__(
        self,
        room: Room,
        *,
        interval_s: float = HEARTBEAT_INTERVAL_S,
        room_name: str | None = None,
    ) -> None:
        self._room = room
        self._interval = interval_s
        self._room_name = room_name
        self._task: asyncio.Task | None = None

    async def _loop(self) -> None:
        try:
            while True:
                payload = heartbeat_payload(room=self._room_name)
                data = json.dumps(payload).encode("utf-8")
                try:
                    await self._room.local_participant.publish_data(
                        payload=data, topic=TOPIC_HEARTBEAT
                    )
                except Exception as e:  # noqa: BLE001 — never let a tick kill the loop
                    logger.warning("[heartbeat] publish failed: %s", e)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            raise

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="vh-heartbeat")
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


class BackchannelWatcher:
    """Auto-fires a 3-word backchannel when user speaks >6s w/o agent reply."""

    def __init__(
        self,
        session: AgentSession,
        *,
        after_seconds: float = BACKCHANNEL_AFTER_SECONDS,
        phrases: list[str] | None = None,
    ) -> None:
        self._session = session
        self._after = after_seconds
        self._phrases = list(phrases or BACKCHANNEL_CYCLE)
        self._idx = 0

    def should_fire(self, *, user_speaking_secs: float, agent_recently_spoke: bool) -> bool:
        return user_speaking_secs >= self._after and not agent_recently_spoke

    def fire_next(self) -> str:
        phrase = self._phrases[self._idx % len(self._phrases)]
        self._idx += 1
        logger.info("[backchannel] %s", phrase)
        # backchannel = non-interrupting, do NOT take the floor
        self._session.say(phrase, allow_interruptions=True)
        return phrase
