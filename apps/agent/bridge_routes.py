"""HTTPS bridge for agents without WebRTC (cloud sandboxes behind a CONNECT proxy).

Everything works with plain curl. The session token is a bearer secret and only
ever travels in the `Authorization: Bearer <session>` header, never in a URL.

    POST /api/bridge/join    {invite_url | room [+ invite], name, model, identity?,
                              greet?, persona?, force_persona?, idle_timeout?}
                             -> {session, expires_in, room, identity, idle_timeout_s}
    GET  /api/bridge/next?timeout=60   one JSON object like CLI 0.5.0 `next`
    POST /api/bridge/say     {text, mode?}            like CLI `say`
    POST /api/bridge/leave   {say?}                   like CLI `leave`
    GET  /api/bridge/status                           like CLI `status`
    GET  /api/bridge/events  Server-Sent Events: every data packet (topic, payload,
                             sender), peer-joined/left, speakers, track, room-state, ended
    POST /api/bridge/send    {topic, payload, force?} raw publish (allowlisted topics)

Authorization of the join is exactly the CLI's: the server mints the same
operator token as `GET /api/token?invite=1&name=..&model=..` (vh.role=agent,
vh.name, vh.model; active-room check; voice-ai dispatch only for rooms with a
known payer). If the invite URL carries an HMAC `?invite=`, it is verified too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import time
from typing import Annotated
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import bridge
from .billing_routes import client_ip
from .tokens import verify_invite

logger = logging.getLogger("voicehook.bridge")

router = APIRouter()

_ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class JoinRequest(BaseModel):
    invite_url: str | None = Field(None, max_length=2048)
    room: str | None = Field(None, max_length=200)
    invite: str | None = Field(None, max_length=512)
    name: str = Field(..., min_length=1, max_length=200)
    model: str = Field(..., min_length=1, max_length=200)
    identity: str | None = Field(None, max_length=128)
    greet: str | None = Field(None, max_length=2000)
    persona: str | None = Field(None, max_length=12000)
    force_persona: bool = False
    idle_timeout: float = Field(bridge.DEFAULT_IDLE_MIN, ge=0, le=bridge.MAX_IDLE_MIN)


class SayRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    mode: str | None = None


class LeaveRequest(BaseModel):
    say: str | None = Field(None, max_length=4000)


class SendRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)
    force: bool = False


def _room_and_invite(req: JoinRequest) -> tuple[str, str | None]:
    room, invite = req.room, req.invite
    if req.invite_url:
        u = urlparse(req.invite_url.strip())
        m = re.search(r"/r/([^/?#]+)", u.path or "")
        if m:
            room = room or m.group(1)
        elif not u.scheme and _ROOM_RE.match(req.invite_url.strip()):
            room = room or req.invite_url.strip()  # bare slug, like the CLI
        q = parse_qs(u.query or "")
        if not invite and q.get("invite"):
            invite = q["invite"][0]
    if not room or not _ROOM_RE.match(room):
        raise HTTPException(status_code=400, detail="no valid room in invite_url/room")
    return room, invite


def _identity(req: JoinRequest) -> str:
    if req.identity:
        if not _IDENT_RE.match(req.identity):
            raise HTTPException(status_code=400, detail="identity: only [A-Za-z0-9._-]")
        return req.identity
    base = re.sub(r"[^a-z0-9]", "", req.name.lower())[:16] or "agent"
    return f"{base}-bridge-{secrets.token_hex(2)}"


def _bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _session(request: Request) -> bridge.Session:
    tok = _bearer(request)
    if not tok:
        raise HTTPException(status_code=401, detail="missing Authorization: Bearer <session>")
    s = bridge.REGISTRY.get(tok)
    if s is None:
        raise HTTPException(status_code=404, detail="unknown or expired bridge session")
    return s


SessionDep = Annotated[bridge.Session, Depends(_session)]


def _live(s: bridge.Session) -> bridge.Session:
    if s.ended:
        raise HTTPException(status_code=410, detail=f"session ended ({s.end_reason})")
    return s


@router.post("/api/bridge/join")
async def bridge_join(req: JoinRequest, request: Request) -> dict:
    from . import server as srv  # noqa: PLC0415  (server includes this router)

    ip = client_ip(request)
    room_name, invite = _room_and_invite(req)
    if not bridge.REGISTRY.join_allowed(ip):
        raise HTTPException(status_code=429, detail="too many joins, try again later")
    err = bridge.REGISTRY.capacity_error(room_name, ip)
    if err:
        raise HTTPException(status_code=429, detail=err)
    if invite and invite != "1":
        verdict = verify_invite(invite, room_name)
        if not verdict.valid:
            raise HTTPException(status_code=403, detail=f"invalid invite: {verdict.reason}")
    identity = _identity(req)
    # Same token + same gates as the CLI's GET /api/token?invite=1 (410 ended room,
    # 503 no LK creds, dispatch only for rooms with a payer).
    tok = await asyncio.to_thread(
        srv.issue_token_get, room=room_name, identity=identity, invite="1",
        ttl_seconds=3600, name=req.name, model=req.model,
    )
    lk_url = os.environ.get("VOICEHOOK_BRIDGE_LIVEKIT_URL") or tok.url
    slot = (room_name, ip)
    bridge.REGISTRY.pending.append(slot)
    room = bridge.ROOM_FACTORY()
    token, token_hash = bridge.REGISTRY.new_token()
    s = bridge.Session(token_hash=token_hash, room_name=room_name, identity=identity, ip=ip,
                       room=room, idle_timeout_s=req.idle_timeout * 60.0,
                       max_s=bridge.max_call_seconds())
    bridge.wire(s)
    try:
        await asyncio.wait_for(room.connect(lk_url, tok.token, bridge.ROOM_OPTIONS()),
                               timeout=bridge.CONNECT_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        logger.warning("[bridge] connect failed room=%s: %s", room_name, type(e).__name__)
        with contextlib.suppress(Exception):
            await room.disconnect()
        raise HTTPException(status_code=502, detail="livekit connect failed") from None
    finally:
        with contextlib.suppress(ValueError):
            bridge.REGISTRY.pending.remove(slot)
    bridge.REGISTRY.add(s)
    logger.info("[bridge] join room=%s identity=%s ip=%s", room_name, identity, ip)
    notes = []
    if req.persona:
        others = bridge.other_operators(room)
        if others and not req.force_persona:
            notes.append(f"persona NOT pushed: other operator agent in the room ({', '.join(others)})")
        else:
            await s.publish("operator.persona", {"text": req.persona})
            notes.append("persona pushed")
    if req.greet:
        await s.say(req.greet)
        notes.append("greet pushed")
    s.guard_task = asyncio.create_task(bridge.guard(s))
    return {"session": token, "expires_in": int(s.max_s), "room": room_name,
            "identity": identity, "idle_timeout_s": s.idle_timeout_s,
            "peers": s.peers(), "notes": notes}


@router.get("/api/bridge/next")
async def bridge_next(s: SessionDep, timeout: float = 60.0) -> dict:
    timeout = max(0.0, min(float(timeout), 120.0))
    s.touch()
    s.busy += 1
    try:
        ev = await s.events.get(timeout)
    finally:
        s.busy -= 1
        s.touch()
    if ev is None:
        return {"ok": True, "type": "timeout", "pending": 0}
    return {"ok": True, **ev, "pending": len(s.events)}


@router.post("/api/bridge/say")
async def bridge_say(req: SayRequest, s: SessionDep) -> dict:
    _live(s)
    s.touch()
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if req.mode and req.mode not in bridge.SAY_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {bridge.SAY_MODES}")
    if not s.send_allowed():
        raise HTTPException(status_code=429, detail="too many sends")
    seq = await s.say(text, req.mode)
    return {"ok": True, "seq": seq}


@router.post("/api/bridge/send")
async def bridge_send(req: SendRequest, s: SessionDep) -> dict:
    _live(s)
    s.touch()
    if req.topic not in bridge.SEND_TOPICS:
        raise HTTPException(status_code=400, detail=f"topic not allowed: {sorted(bridge.SEND_TOPICS)}")
    if len(json.dumps(req.payload, ensure_ascii=False).encode("utf-8")) > bridge.MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    if req.topic in bridge.PERSONA_TOPICS and not req.force:
        others = bridge.other_operators(s.room)
        if others:
            raise HTTPException(status_code=409, detail=(
                f"persona/mode NOT pushed: other operator agent in the room ({', '.join(others)}); "
                "pass force:true to override"))
    if not s.send_allowed():
        raise HTTPException(status_code=429, detail="too many sends")
    payload = s.tag_say(req.payload) if req.topic == "operator.say" else req.payload
    await s.publish(req.topic, payload)
    return {"ok": True, **({"seq": payload["_seq"]} if req.topic == "operator.say" else {})}


@router.post("/api/bridge/leave")
async def bridge_leave(s: SessionDep, req: LeaveRequest | None = None) -> dict:
    if s.ended:
        return {"ok": True, "type": "ended", "reason": s.end_reason}
    text = ((req.say if req else None) or "").strip()
    if text:
        with contextlib.suppress(Exception):
            await s.say(text, "append")
            await asyncio.sleep(0.5)  # let the reliable packet leave before we disconnect
    await s.end("left")
    return {"ok": True, "type": "leaving"}


@router.get("/api/bridge/status")
async def bridge_status(s: SessionDep) -> dict:
    return {"ok": True, "type": "status", "room": s.room_name, "identity": s.identity,
            "connected": not s.ended, "ended_reason": s.end_reason, "pending": len(s.events),
            "idle_s": round(s.idle_for(), 1), "idle_timeout_s": s.idle_timeout_s,
            "sse_clients": len(s.subs), "peers": s.peers()}


def _sse(ev: dict) -> bytes:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode()


@router.get("/api/bridge/events")
async def bridge_events(s: SessionDep) -> StreamingResponse:
    q = s.add_sub()

    async def gen():
        try:
            yield _sse({"type": "hello", "room": s.room_name, "identity": s.identity,
                        "expires_in": max(0, int(s.expires_at - time.monotonic())),
                        "peers": s.peers()})
            yield _sse(s.room_state())
            if s.ended:
                yield _sse({"type": "ended", "reason": s.end_reason})
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=bridge.SSE_PING_S)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue
                yield _sse(ev)
                if ev.get("type") == "ended":
                    return
        finally:
            s.remove_sub(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
