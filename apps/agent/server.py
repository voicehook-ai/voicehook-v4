"""FastAPI surface for the voicehook agent.

PR-2 scope: HMAC-gated /api/token mint + /healthz. PR-6 added /status.
PR-12 adds belt-and-suspenders agent dispatch: on every POST /api/token we
fire `AgentDispatchService.CreateDispatch` for the room. JWT `roomConfig.agents`
auto-dispatches on FIRST participant join, but if the room was already created
by a senior peer (no agents claim), that path is dead — the explicit dispatch
guarantees an agent shows up either way. Idempotent server-side (LK dedups).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .health import probe_all
from .tokens import mint_livekit_token, verify_invite

logger = logging.getLogger("voicehook.server")

app = FastAPI(title="voicehook-agent", version="4.0.0-dev")


class TokenRequest(BaseModel):
    room: str = Field(..., min_length=1, max_length=200)
    identity: str = Field(..., min_length=1, max_length=200)
    invite: str = Field(..., min_length=1, max_length=512)
    ttl_seconds: int = Field(3600, ge=60, le=86400)


class TokenResponse(BaseModel):
    token: str
    url: str
    room: str
    identity: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe. PR-6 adds functional probes (STT/TTS/LLM)."""
    return {"status": "ok"}


@app.get("/status")
def status() -> dict:
    """Functional health: each component's probe. 200 always; clients read `healthy`.

    Component checks today are env-presence (key/credentials). PR-10 promotes
    these to real round-trip probes (Deepgram WS handshake, TTS synthesize a
    tone, Gemini ping prompt) so /status reflects upstream availability, not
    just configuration.
    """
    probe = probe_all()
    return {
        "service": "voicehook-agent",
        "version": "4.0.0-dev",
        "probe": {"stt": probe.stt, "tts": probe.tts, "llm": probe.llm},
        "healthy": probe.healthy,
    }


def _lk_admin_jwt(api_key: str, api_secret: str, room: str) -> str:
    """Mint a 60s admin JWT for the LK twirp API (used for CreateDispatch)."""
    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")
    h = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64u(json.dumps({"iss": api_key, "exp": int(time.time()) + 60,
                          "video": {"roomAdmin": True, "room": room}}).encode())
    sig = b64u(hmac.new(api_secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())
    return f"{h}.{p}.{sig}"


def _ensure_agent_dispatched(room: str, agent_name: str = "voice-ai") -> None:
    """Belt-and-suspenders: fire CreateDispatch for the room. Idempotent — LK dedups
    when there's already a worker assigned for this room/agent_name."""
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
    if not api_key or not api_secret:
        return
    # twirp lives on the HTTP port — for in-cluster calls hit local LK directly
    http_url = livekit_url.replace("wss://", "https://").replace("ws://", "http://")
    # local-machine optimization: use loopback (avoids TLS dance)
    if "127.0.0.1" in livekit_url or "localhost" in livekit_url:
        http_url = "http://127.0.0.1:7880"
    elif livekit_url.startswith(("wss://rtc.", "ws://rtc.")):
        http_url = "http://127.0.0.1:7880"  # same box, direct
    try:
        req = urllib.request.Request(
            f"{http_url}/twirp/livekit.AgentDispatchService/CreateDispatch",
            method="POST",
            data=json.dumps({"room": room, "agent_name": agent_name}).encode(),
            headers={"Authorization": f"Bearer {_lk_admin_jwt(api_key, api_secret, room)}",
                     "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3).read()
    except (urllib.error.URLError, TimeoutError) as e:
        logger.warning("[ensure-dispatch] %s: %s", room, e)


def _mint(room: str, identity: str, invite: str, ttl_seconds: int, *, agent_name: str | None = "voice-ai") -> TokenResponse:
    verdict = verify_invite(invite, room)
    if not verdict.valid:
        raise HTTPException(status_code=403, detail=f"invalid invite: {verdict.reason}")
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
    if not api_key or not api_secret:
        raise HTTPException(status_code=503, detail="server missing LiveKit credentials")
    token = mint_livekit_token(
        api_key=api_key, api_secret=api_secret,
        room=room, identity=identity, ttl_seconds=ttl_seconds,
        agent_name=agent_name,
    )
    # Fire explicit dispatch off the request path so the token returns fast
    # even if LK is slow; the agent shows up shortly after the participant
    # connects. Threading (not asyncio) to avoid event-loop coupling with the
    # sync FastAPI route handler.
    if agent_name:
        import threading
        threading.Thread(
            target=_ensure_agent_dispatched, args=(room, agent_name), daemon=True
        ).start()
    return TokenResponse(token=token, url=livekit_url, room=room, identity=identity)


@app.post("/api/token", response_model=TokenResponse)
def issue_token(req: TokenRequest) -> TokenResponse:
    """Mint a LK JWT — POST flow, HMAC invite required. Auto-dispatches voice-ai."""
    return _mint(req.room, req.identity, req.invite, req.ttl_seconds)


@app.get("/api/token", response_model=TokenResponse)
def issue_token_get(
    room: str, identity: str, invite: str = "", ttl_seconds: int = 3600
) -> TokenResponse:
    """GET-flavor compat for voicehook-agent CLI (v3 protocol).

    The CLI passes `invite=1` for senior peers (no agent re-dispatch needed since
    voice-ai is already in the room). For that special value we mint a plain
    join token (agent_name=None). Otherwise we require a real HMAC invite.
    """
    if invite == "1":
        # senior peer — no HMAC, no auto-dispatch (room is already live)
        api_key = os.environ.get("LIVEKIT_API_KEY")
        api_secret = os.environ.get("LIVEKIT_API_SECRET")
        livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
        if not api_key or not api_secret:
            raise HTTPException(status_code=503, detail="server missing LiveKit credentials")
        token = mint_livekit_token(
            api_key=api_key, api_secret=api_secret,
            room=room, identity=identity, ttl_seconds=ttl_seconds,
            agent_name=None,
        )
        return TokenResponse(token=token, url=livekit_url, room=room, identity=identity)
    return _mint(room, identity, invite, ttl_seconds)
