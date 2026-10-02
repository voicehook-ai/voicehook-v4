"""HTTPS bridge core: the server joins a LiveKit room on behalf of an agent.

Why: agents in cloud sandboxes (claude.ai/code and similar) only get HTTPS out
through an HTTP CONNECT proxy. libwebrtc ignores that proxy and LiveKit on the
box has no TURN, so the CLI's WebRTC join times out (`wait_pc_connection timed
out`). Agents need no audio, only the data-channel topics. So the box (which
has network) joins as a normal participant with the SAME token the CLI would
get (`GET /api/token?invite=1`, attributes vh.role/vh.name/vh.model) and relays
the data channel over plain HTTPS: SSE or long-poll down, POST up.

This module holds the room-facing state (sessions, queues, guards). The HTTP
surface is `bridge_routes.py`. No secrets and no spoken content are logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("voicehook.bridge")

# Topics an agent may publish through the bridge (what the CLI sends today).
# operator.graph is CLI-local (it becomes operator.persona) and never hits the wire.
SEND_TOPICS = frozenset({
    "operator.say",
    "operator.persona",
    "operator.mode",
    "operator.interrupt",
    "operator.inject",
    "operator.backchannel",
    "operator.status",      # Status-Board {doing, open[], done[]} (apps/agent/board.py)
})
PERSONA_TOPICS = frozenset({"operator.persona", "operator.mode"})
SAY_MODES = ("revise", "overwrite", "append")
MAX_PAYLOAD_BYTES = 15_000  # LiveKit reliable data packets top out around 15 KiB
STATUS_STALE_S = 300.0      # `next` meldet status_stale, wenn das Board älter ist und seither gesprochen wurde

# Tunables (module-level so tests can shrink them).
SSE_PING_S = 15.0          # SSE comment heartbeat
SSE_GRACE_S = 60.0         # SSE client gone longer than this -> leave the room
GUARD_TICK_S = 1.0
ENDED_KEEP_S = 300.0       # ended sessions still answer next -> {"type":"ended"}
CONNECT_TIMEOUT_S = 20.0
DEFAULT_IDLE_MIN = 10.0
MAX_IDLE_MIN = 60.0
SEND_LIMIT = 20            # sends (say/send) per SEND_WINDOW_S per session
SEND_WINDOW_S = 10.0
MAX_SESSIONS_PER_ROOM = 4
MAX_SESSIONS_PER_IP = 4
MAX_SESSIONS_TOTAL = 100
JOIN_LIMIT = 10            # join attempts per IP per JOIN_WINDOW_S
JOIN_WINDOW_S = 60.0
SSE_QUEUE_MAX = 1000
EVENT_QUEUE_MAX = 200

DEFAULT_IDLE_SAY = (
    "Ich verlasse den Call jetzt, weil ich von meinem Agenten seit einer Weile "
    "nichts mehr hoere. Lade mich gern wieder ein."
)


def max_call_seconds() -> float:
    """Same cap as the worker's CallGuard (VH_MAX_CALL_SECONDS, default 3600)."""
    try:
        v = float(os.environ.get("VH_MAX_CALL_SECONDS", "3600"))
    except ValueError:
        return 3600.0
    return v if v > 0 else 3600.0


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def kind_label(kind: Any) -> str:
    """LK ParticipantKind -> label, same mapping as the CLI."""
    try:
        k = int(kind)
    except (TypeError, ValueError):
        return "user"
    return {0: "user", 1: "ingress", 2: "egress", 3: "sip", 4: "agent"}.get(k, f"kind{k}")


def _kind_int(p: Any) -> int:
    try:
        return int(getattr(p, "kind", 0))
    except (TypeError, ValueError):
        return 0


def _has_audio(p: Any) -> bool:
    for pub in (getattr(p, "track_publications", None) or {}).values():
        if int(getattr(pub, "kind", 0)) == 1 and not getattr(pub, "muted", False):
            return True
    return False


def peer_info(p: Any, speakers: set[str] | None = None) -> dict:
    attrs = dict(getattr(p, "attributes", None) or {})
    return {
        "identity": p.identity,
        "kind": _kind_int(p),
        "kind_label": kind_label(_kind_int(p)),
        "name": getattr(p, "name", "") or "",
        "attributes": attrs,
        "audio": _has_audio(p),
        "speaking": p.identity in (speakers or set()),
        "operator": attrs.get("vh.role") == "agent" and _kind_int(p) != 4,
    }


def other_operators(room: Any) -> list[str]:
    """Other operator agents in the room (vh.role=agent, not the voice-ai worker)."""
    return sorted(
        p.identity for p in room.remote_participants.values()
        if _kind_int(p) != 4 and (dict(getattr(p, "attributes", None) or {})).get("vh.role") == "agent"
    )


def _is_final(payload: dict) -> bool:
    for k in ("final", "is_final", "isFinal"):
        if k in payload:
            return bool(payload[k])
    return True


def user_turn_event(payload: dict) -> dict | None:
    """Same shape as CLI 0.5.0 `next` for a finalized user turn."""
    if payload.get("role") != "user" or not _is_final(payload):
        return None
    text = str(payload.get("text") or "").strip()
    if not text:
        return None
    return {"type": "user", "role": "user", "text": text, "ts": time.time()}


def revise_event(payload: dict) -> dict:
    ev = {"type": "revise", "role": "system", "text": payload.get("text", ""), "ts": time.time()}
    for k in ("unspoken", "new"):
        if k in payload:
            ev[k] = payload[k]
    return ev


AGENT_SAID_MAX = 3          # entries in `agent_said`
AGENT_SAID_CHARS = 400      # total chars in `agent_said`


class AgentSaid:
    """What the voicebot (Delta) said on its own since the last `next` (transcript
    role=agent, never echoes of your own say). `next` hands it out as `agent_said`
    so you do not repeat it and can correct it. Oldest entries drop first."""

    def __init__(self) -> None:
        self._items: list[str] = []

    def add(self, text: str) -> None:
        text = " ".join(str(text or "").split())
        if not text:
            return
        self._items.append(text[:AGENT_SAID_CHARS])
        del self._items[:-AGENT_SAID_MAX]
        while sum(map(len, self._items)) > AGENT_SAID_CHARS and len(self._items) > 1:
            self._items.pop(0)

    def take(self) -> dict:
        """{"agent_said": [...]} (chronological) and reset, or {} when nothing new."""
        if not self._items:
            return {}
        out, self._items = self._items, []
        return {"agent_said": out}


class EventQueue:
    """FIFO for `next` (user turns, operator.revise, final `ended`). Bounded."""

    def __init__(self, maxlen: int = EVENT_QUEUE_MAX) -> None:
        self._q: deque[dict] = deque(maxlen=maxlen)
        self._cond = asyncio.Condition()
        self.closed = False
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._q)

    def put_nowait(self, ev: dict) -> None:
        if len(self._q) == self._q.maxlen:
            self.dropped += 1
        self._q.append(ev)
        asyncio.get_running_loop().create_task(self._notify())

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def close(self) -> None:
        async with self._cond:
            self.closed = True
            self._cond.notify_all()

    async def get(self, timeout: float) -> dict | None:
        if timeout <= 0:
            if self._q:
                return self._q.popleft()
            return {"type": "ended"} if self.closed else None

        async def _wait() -> dict:
            async with self._cond:
                while not self._q and not self.closed:
                    await self._cond.wait()
                return self._q.popleft() if self._q else {"type": "ended"}
        try:
            return await asyncio.wait_for(_wait(), timeout=timeout)
        except TimeoutError:
            return None


class Session:
    """One agent in one room, held by the server."""

    def __init__(self, *, token_hash: str, room_name: str, identity: str, ip: str,
                 room: Any, idle_timeout_s: float, max_s: float) -> None:
        self.token_hash = token_hash
        self.room_name = room_name
        self.identity = identity
        self.ip = ip
        self.room = room
        self.created = time.monotonic()
        self.expires_at = self.created + max_s
        self.max_s = max_s
        self.idle_timeout_s = idle_timeout_s
        self.last_active = self.created     # last say/next/send (idle guard)
        self.busy = 0                       # running `next` calls count as alive
        self.subs: set[asyncio.Queue] = set()
        self.sse_seen = False
        self.sse_gone_at: float | None = None
        self.events = EventQueue()
        self.speakers: set[str] = set()
        self.seq = 0
        self.sends: deque[float] = deque()
        self.ended = False
        self.ended_at: float | None = None
        self.end_reason: str | None = None
        self._last_state: tuple | None = None
        self.guard_task: asyncio.Task | None = None
        self.loop = asyncio.get_running_loop()  # LiveKit callbacks + guard run here
        self.status_at = self.created          # letztes operator.status (Join zählt als Start)
        self.user_at: float | None = None      # letzter Nutzer-Turn
        self.agent_said = AgentSaid()          # Deltas eigene Sätze seit dem letzten `next`

    # ---- liveness ------------------------------------------------------- #
    def touch(self) -> None:
        self.last_active = time.monotonic()

    def idle_for(self) -> float:
        return 0.0 if self.busy else max(0.0, time.monotonic() - self.last_active)

    def send_allowed(self) -> bool:
        now = time.monotonic()
        while self.sends and now - self.sends[0] > SEND_WINDOW_S:
            self.sends.popleft()
        if len(self.sends) >= SEND_LIMIT:
            return False
        self.sends.append(now)
        return True

    # ---- SSE fan-out ---------------------------------------------------- #
    def add_sub(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE_MAX)
        self.subs.add(q)
        self.sse_seen = True
        self.sse_gone_at = None
        return q

    def remove_sub(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)
        if not self.subs:
            self.sse_gone_at = time.monotonic()

    def broadcast(self, ev: dict) -> None:
        for q in list(self.subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:  # slow client: drop the oldest, keep the newest
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(ev)

    def peers(self) -> list[dict]:
        out = [peer_info(p, self.speakers) for p in self.room.remote_participants.values()]
        return sorted(out, key=lambda d: d["identity"])

    def room_state(self) -> dict:
        return {"type": "room-state", "peers": self.peers()}

    def maybe_room_state(self) -> None:
        st = self.room_state()
        sig = tuple((d["identity"], d["kind"], d["audio"], d["speaking"]) for d in st["peers"])
        if sig != self._last_state:
            self._last_state = sig
            self.broadcast(st)

    # ---- publishing ----------------------------------------------------- #
    def tag_say(self, payload: dict) -> dict:
        """CLI envelope: {text, _seq, _ts, mode?...}."""
        if "_seq" in payload:
            return payload
        self.seq += 1
        env = {"text": payload.get("text", ""), "_seq": self.seq, "_ts": time.time()}
        env.update({k: v for k, v in payload.items() if k != "text"})
        return env

    async def publish(self, topic: str, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self.room.local_participant.publish_data(data, reliable=True, topic=topic)
        if topic == "operator.status":
            self.status_at = time.monotonic()

    def status_stale(self, now: float | None = None) -> bool:
        """Board älter als STATUS_STALE_S und seither hat der Nutzer gesprochen."""
        now = time.monotonic() if now is None else now
        return (self.user_at is not None and self.user_at > self.status_at
                and now - self.status_at > STATUS_STALE_S)

    async def say(self, text: str, mode: str | None = None) -> int:
        extra = {"mode": mode} if mode else {}
        env = self.tag_say({"text": text, **extra})
        await self.publish("operator.say", env)
        return int(env["_seq"])

    # ---- end ------------------------------------------------------------ #
    async def end(self, reason: str, *, disconnect: bool = True) -> None:
        if self.ended:
            return
        self.ended = True
        self.ended_at = time.monotonic()
        self.end_reason = reason
        logger.info("[bridge] end room=%s identity=%s reason=%s", self.room_name, self.identity, reason)
        await self.events.close()
        self.broadcast({"type": "ended", "reason": reason})
        if disconnect:
            with contextlib.suppress(Exception):
                await self.room.disconnect()
        if self.guard_task is not None and self.guard_task is not asyncio.current_task():
            self.guard_task.cancel()


def wire(session: Session) -> None:
    """Hook the room's events into SSE + the `next` queue (what the CLI listens to)."""
    room = session.room

    def _on_data(pkt: Any) -> None:
        topic = (getattr(pkt, "topic", "") or "").strip()
        try:
            payload = json.loads(bytes(pkt.data).decode("utf-8"))
        except Exception:  # noqa: BLE001  CLI drops non-JSON packets too
            return
        part = getattr(pkt, "participant", None)
        sender = getattr(part, "identity", None) if part is not None else None
        session.broadcast({"type": "data", "topic": topic, "payload": payload, "sender": sender})
        if not isinstance(payload, dict):
            return
        if topic == "transcript":
            if payload.get("role") == "agent" and _is_final(payload):
                session.agent_said.add(payload.get("text", ""))
            ev = user_turn_event(payload)
            if ev is not None:
                session.user_at = time.monotonic()
                session.events.put_nowait(ev)
        elif topic == "operator.revise":
            session.events.put_nowait(revise_event(payload))
        elif topic == "operator.status_request":
            session.events.put_nowait({"type": "status_request", "role": "system",
                                       "text": str(payload.get("text", ""))[:200], "ts": time.time()})

    def _peer(kind: str) -> Callable[[Any], None]:
        def _h(p: Any) -> None:
            if kind == "peer-left":
                session.speakers.discard(p.identity)
            session.broadcast({"type": kind, **peer_info(p, session.speakers)})
            session.maybe_room_state()
        return _h

    def _on_attrs(changed: Any, p: Any) -> None:
        session.broadcast({"type": "peer-updated", **peer_info(p, session.speakers)})

    def _on_speakers(spk: Any) -> None:
        session.speakers = {s.identity for s in spk}
        session.broadcast({"type": "speakers", "speakers": sorted(session.speakers)})
        session.maybe_room_state()

    def _track(state: str, pub_first: bool) -> Callable[..., None]:
        def _h(a: Any, b: Any, *_: Any) -> None:
            pub, p = (a, b) if pub_first else (b, a)
            if int(getattr(pub, "kind", 0)) != 1:
                return
            session.broadcast({"type": "track", "identity": p.identity, "state": state})
            session.maybe_room_state()
        return _h

    def _on_disconnected(*args: Any) -> None:
        reason = args[0] if args else None
        try:
            from livekit import rtc  # noqa: PLC0415
            name = rtc.DisconnectReason.Name(int(reason)) if reason is not None else "UNKNOWN"
        except Exception:  # noqa: BLE001
            name = str(reason) if reason is not None else "UNKNOWN"
        asyncio.get_running_loop().create_task(session.end(name, disconnect=False))

    room.on("data_received", _on_data)
    room.on("participant_connected", _peer("peer-joined"))
    room.on("participant_disconnected", _peer("peer-left"))
    room.on("participant_attributes_changed", _on_attrs)
    room.on("active_speakers_changed", _on_speakers)
    room.on("track_published", _track("on", True))
    room.on("track_unpublished", _track("off", True))
    room.on("track_muted", _track("mute", False))
    room.on("track_unmuted", _track("unmute", False))
    room.on("reconnecting", lambda *_: session.broadcast({"type": "reconnecting"}))
    room.on("reconnected", lambda *_: session.broadcast({"type": "reconnected"}))
    room.on("disconnected", _on_disconnected)


async def guard(session: Session, idle_say: str | None = DEFAULT_IDLE_SAY) -> None:
    """Max duration (CallGuard cap), idle guard (no say/next) and SSE-gone guard."""
    try:
        while not session.ended:
            await asyncio.sleep(GUARD_TICK_S)
            now = time.monotonic()
            if now >= session.expires_at:
                await session.end("max_duration")
                return
            if session.idle_timeout_s > 0 and session.idle_for() >= session.idle_timeout_s:
                if idle_say:
                    with contextlib.suppress(Exception):
                        await session.say(idle_say, "append")
                        await asyncio.sleep(min(1.0, GUARD_TICK_S * 5))
                await session.end("idle_timeout")
                return
            if (session.sse_seen and not session.subs and session.sse_gone_at is not None
                    and now - session.sse_gone_at >= SSE_GRACE_S):
                await session.end("sse_gone")
                return
            session.maybe_room_state()
    except asyncio.CancelledError:
        pass


class Registry:
    """In-process session store (the API runs as one uvicorn process)."""

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.pending: list[tuple[str, str]] = []  # (room, ip) reserved during connect
        self.joins: dict[str, deque[float]] = {}

    def purge(self) -> None:
        now = time.monotonic()
        for h, s in list(self.sessions.items()):
            if s.ended and s.ended_at is not None and now - s.ended_at > ENDED_KEEP_S:
                self.sessions.pop(h, None)

    def active(self) -> list[Session]:
        return [s for s in self.sessions.values() if not s.ended]

    def join_allowed(self, ip: str) -> bool:
        now = time.monotonic()
        dq = self.joins.setdefault(ip, deque())
        while dq and now - dq[0] > JOIN_WINDOW_S:
            dq.popleft()
        if len(dq) >= JOIN_LIMIT:
            return False
        dq.append(now)
        if len(self.joins) > 5000:
            self.joins = {k: v for k, v in self.joins.items() if v}
        return True

    def capacity_error(self, room: str, ip: str) -> str | None:
        self.purge()
        act = [(s.room_name, s.ip) for s in self.active()] + self.pending
        if len(act) >= MAX_SESSIONS_TOTAL:
            return "bridge is full, try again later"
        if sum(1 for r, _ in act if r == room) >= MAX_SESSIONS_PER_ROOM:
            return "too many bridge sessions in this room"
        if sum(1 for _, i in act if i == ip) >= MAX_SESSIONS_PER_IP:
            return "too many bridge sessions from this address"
        return None

    def new_token(self) -> tuple[str, str]:
        tok = secrets.token_urlsafe(32)
        return tok, hash_token(tok)

    def add(self, s: Session) -> None:
        self.sessions[s.token_hash] = s

    def get(self, token: str) -> Session | None:
        if not token:
            return None
        self.purge()
        return self.sessions.get(hash_token(token))


REGISTRY = Registry()


def default_room_factory() -> Any:
    from livekit import rtc  # noqa: PLC0415  heavy import only when used

    return rtc.Room()


def default_room_options() -> Any:
    from livekit import rtc  # noqa: PLC0415

    # No audio needed: never subscribe (no RTP to the box process).
    return rtc.RoomOptions(auto_subscribe=False)


ROOM_FACTORY: Callable[[], Any] = default_room_factory
ROOM_OPTIONS: Callable[[], Any] = default_room_options
