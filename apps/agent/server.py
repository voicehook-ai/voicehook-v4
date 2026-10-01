"""FastAPI surface for the voicehook agent.

PR-2 scope: HMAC-gated /api/token mint + /healthz. PR-6 added /status.
PR-12 adds belt-and-suspenders agent dispatch: on every POST /api/token we
fire `AgentDispatchService.CreateDispatch` for the room. JWT `roomConfig.agents`
auto-dispatches on FIRST participant join, but if the room was already created
by a operator peer (no agents claim), that path is dead — the explicit dispatch
guarantees an agent shows up either way. Idempotent server-side (LK dedups).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import billing_routes, budget, freetier
from .health import probe_all
from .slug import gen_slug
from .tokens import mint_invite, mint_livekit_token, verify_invite

logger = logging.getLogger("voicehook.server")

app = FastAPI(title="voicehook-agent", version="4.0.0-dev")
app.include_router(billing_routes.router)  # Aufladen + Wallet (/api/checkout, /api/wallet, ...)


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


# Per-room locks serialize concurrent ensure-dispatch calls (operator invite=1 +
# user invite-mint can race → two voice-ai workers, #47). The lock makes the
# list-then-create check atomic within the process.
_DISPATCH_LOCKS: dict[str, threading.Lock] = {}
_DISPATCH_LOCKS_GUARD = threading.Lock()


def _room_lock(room: str) -> threading.Lock:
    with _DISPATCH_LOCKS_GUARD:
        return _DISPATCH_LOCKS.setdefault(room, threading.Lock())


# ----- Gemini-Live-Testmodus ------------------------------------------------
# Ein eigener Worker (`voice-ai-live`, VOICEHOOK_PIPELINE=live) übernimmt nur
# Räume, die über POST /api/admin/live-room (Schlüssel im
# Authorization-Header, NIE in einer URL) oder öffentlich über POST /api/live-room
# (Demo, siehe unten) angelegt werden. Zurück kommt ein normaler, raum-
# gebundener und ablaufender Einladungslink. Die Zuordnung Raum -> Worker gilt
# für JEDEN späteren Dispatch (Operator-Join, Invites): nie zwei Agents im Raum.
# Ohne VOICEHOOK_LIVE_KEY auf dem Server ist der Admin-Endpunkt aus (404).
LIVE_AGENT_NAME = os.environ.get("VOICEHOOK_LIVE_AGENT_NAME", "voice-ai-live")
_ROOM_AGENT: dict[str, tuple[str, float]] = {}  # Raum -> (Worker, gültig bis)
_ROOM_AGENT_MAX = 2000
_ROOM_AGENT_MAX_TTL = 86400  # nie länger als die längste Token-TTL; freetier hält free_rooms 7 Tage


def _live_key_ok(given: str) -> bool:
    key = os.environ.get("VOICEHOOK_LIVE_KEY", "")
    return bool(key) and bool(given) and hmac.compare_digest(given, key)


def _set_room_agent(room: str, agent_name: str, ttl_seconds: float = _ROOM_AGENT_MAX_TTL) -> None:
    if len(_ROOM_AGENT) >= _ROOM_AGENT_MAX:  # alte Einträge verwerfen (Prozess-Speicher)
        for k in list(_ROOM_AGENT)[: _ROOM_AGENT_MAX // 2]:
            _ROOM_AGENT.pop(k, None)
    _ROOM_AGENT[room] = (agent_name, time.time() + min(float(ttl_seconds), _ROOM_AGENT_MAX_TTL))


def _agent_for(room: str, default: str = "voice-ai") -> str:
    entry = _ROOM_AGENT.get(room)
    if entry is None:
        return default
    if entry[1] <= time.time():  # Zuordnung abgelaufen (Review 01.10. #2)
        _ROOM_AGENT.pop(room, None)
        return default
    return entry[0]


def _require_active_room(room: str) -> None:
    """Raum mit Wallet-Bindung nur, solange die Bindung aktiv ist (Review 01.10. #1):
    nach Call-Ende oder Ablauf startet kein Join mehr einen Agent auf Kosten des
    Gastgebers. Räume ohne Bindung (Gratis/Operator) laufen wie bisher."""
    b = billing_routes.db.room_binding(room)
    if b is not None and b[2] != "active":
        raise HTTPException(status_code=410, detail="call has ended")


def _ensure_agent_dispatched(room: str, agent_name: str = "voice-ai") -> None:
    """Dispatch the worker assigned to this room (live rooms -> live worker)."""
    _dispatch_now(room, _agent_for(room, agent_name))


_FRESH_DISPATCH_NS = 60 * 10**9   # Agent braucht ein paar Sekunden zum Beitreten (#47)
_ACTIVE_JOB = {"JS_PENDING", "JS_RUNNING", None, ""}


def _dispatch_plan(dispatches: list[dict], agent_name: str, now_ns: int) -> tuple[bool, list[str]]:
    """(keep, stale_ids): keep=True wenn ein lebender/frischer Dispatch existiert."""
    stale = []
    for d in dispatches:
        if d.get("agent_name") != agent_name:
            continue
        state = d.get("state") or {}
        if not state.get("created_at"):
            return True, []  # nicht beurteilbar -> wie bisher: nie doppelt dispatchen (#47)
        jobs = state.get("jobs") or []
        if any((j.get("state") or {}).get("status") in _ACTIVE_JOB for j in jobs):
            return True, []
        created = int(state.get("created_at") or 0)
        if not jobs and now_ns - created < _FRESH_DISPATCH_NS:
            return True, []
        stale.append(d.get("id"))
    return False, [x for x in stale if x]


def _dispatch_now(room: str, agent_name: str) -> None:
    """Ensure exactly one `agent_name` worker is dispatched for the room.

    Presence-idempotent (#47): under a per-room lock, ListDispatch first and skip
    CreateDispatch if a matching dispatch already exists. Plain CreateDispatch is
    NOT enough — two near-simultaneous callers (operator invite=1 + user invite-mint)
    both create before either registers, yielding a double agent."""
    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")
    livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
    if not api_key or not api_secret:
        return
    # twirp lives on the HTTP port — for in-cluster calls hit local LK directly
    http_url = livekit_url.replace("wss://", "https://").replace("ws://", "http://")
    if "127.0.0.1" in livekit_url or "localhost" in livekit_url:
        http_url = "http://127.0.0.1:7880"
    elif livekit_url.startswith(("wss://rtc.", "ws://rtc.")):
        http_url = "http://127.0.0.1:7880"  # same box, direct

    def _twirp(method: str, payload: dict) -> bytes:
        req = urllib.request.Request(
            f"{http_url}/twirp/livekit.AgentDispatchService/{method}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {_lk_admin_jwt(api_key, api_secret, room)}",
                     "Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=3).read()

    with _room_lock(room):
        # already dispatched and alive? skip (closes the race). A dispatch whose
        # job ended (agent left after the idle cap) is stale -> delete + recreate,
        # otherwise the agent would never come back into this room.
        try:
            data = json.loads(_twirp("ListDispatch", {"room": room}) or b"{}")
            keep, stale = _dispatch_plan(data.get("agent_dispatches", []), agent_name, time.time_ns())
            if keep:
                return
            for dispatch_id in stale:
                try:
                    _twirp("DeleteDispatch", {"dispatch_id": dispatch_id, "room": room})
                except (urllib.error.URLError, TimeoutError) as e:
                    logger.warning("[ensure-dispatch] delete stale %s: %s", dispatch_id, e)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            logger.warning("[ensure-dispatch] list %s: %s", room, e)  # fall through to create
        try:
            _twirp("CreateDispatch", {"room": room, "agent_name": agent_name})
        except (urllib.error.URLError, TimeoutError) as e:
            logger.warning("[ensure-dispatch] create %s: %s", room, e)


def _mint(room: str, identity: str, invite: str, ttl_seconds: int, *, agent_name: str | None = "voice-ai") -> TokenResponse:
    verdict = verify_invite(invite, room)
    if not verdict.valid:
        raise HTTPException(status_code=403, detail=f"invalid invite: {verdict.reason}")
    return _issue(room, identity, ttl_seconds, agent_name=agent_name)


def _issue(room: str, identity: str, ttl_seconds: int, *, agent_name: str | None = "voice-ai") -> TokenResponse:
    """Mint + dispatch for an already-authorized room. Skips invite verification —
    callers must have authorized the room themselves (HMAC invite, or a
    server-generated fresh slug in the host-call path)."""
    api_key = os.environ.get("LIVEKIT_API_KEY")
    api_secret = os.environ.get("LIVEKIT_API_SECRET")
    livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
    if not api_key or not api_secret:
        raise HTTPException(status_code=503, detail="server missing LiveKit credentials")
    _require_active_room(room)
    if agent_name:
        agent_name = _agent_for(room, agent_name)  # Live-Räume -> Live-Worker
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
        threading.Thread(
            target=_ensure_agent_dispatched, args=(room, agent_name), daemon=True
        ).start()
    return TokenResponse(token=token, url=livekit_url, room=room, identity=identity)


@app.post("/api/token", response_model=TokenResponse)
def issue_token(req: TokenRequest) -> TokenResponse:
    """Mint a LK JWT — POST flow, HMAC invite required. Auto-dispatches voice-ai.

    Bindet NIE einen Raum an ein Wallet (Review 01.10.): zahlen tut nur, wer den Raum
    über host-call / live-room anlegt, nie ein Gast, der per Einladung beitritt."""
    return _mint(req.room, req.identity, req.invite, req.ttl_seconds)


_LABEL_MAX = 64


def _clean_label(v: str) -> str:
    """Operator self-report label → safe display string: printable only, collapsed
    whitespace, capped at _LABEL_MAX chars (the chip ellipsizes further)."""
    v = "".join(" " if ch.isspace() else ch for ch in (v or "") if ch.isspace() or ch.isprintable())
    return " ".join(v.split())[:_LABEL_MAX]


@app.get("/api/token", response_model=TokenResponse)
def issue_token_get(
    room: str, identity: str, invite: str = "", ttl_seconds: int = 3600,
    name: str = "", model: str = "",
) -> TokenResponse:
    """GET-flavor compat for voicehook-agent CLI (v3 protocol).

    The CLI passes `invite=1` for operator peers — a plain join token (no
    `roomConfig.agents` claim, so the JOIN itself won't dispatch). But the room
    may be agentless (the human entered via a path that never dispatched), and a
    non-developer operator can't be expected to know it must hand-trigger a
    dispatch (#42). So on every invite=1 join we also fire an explicit, idempotent
    CreateDispatch for voice-ai — LK dedups, so it's a no-op if one is already
    assigned. Net: any operator join guarantees voice-ai is in the room, automatically.
    Otherwise we require a real HMAC invite.

    `name` / `model` (operator self-report, CLI --name/--model) land in the JWT
    as LK `name` + `attributes` (vh.role/vh.name/vh.model) so the web presence
    chip can show "Claude · opus-5.5" instead of a guessed brand. Optional —
    older CLIs without them still get a plain token.
    """
    if invite == "1":
        api_key = os.environ.get("LIVEKIT_API_KEY")
        api_secret = os.environ.get("LIVEKIT_API_SECRET")
        livekit_url = os.environ.get("LIVEKIT_URL", "wss://rtc.voicehook.ai")
        if not api_key or not api_secret:
            raise HTTPException(status_code=503, detail="server missing LiveKit credentials")
        _require_active_room(room)  # bekannter Slug allein startet keinen bezahlten Call
        op_name, op_model = _clean_label(name), _clean_label(model)
        attrs = {"vh.role": "agent"}
        if op_name:
            attrs["vh.name"] = op_name
        if op_model:
            attrs["vh.model"] = op_model
        token = mint_livekit_token(
            api_key=api_key, api_secret=api_secret,
            room=room, identity=identity, ttl_seconds=ttl_seconds,
            agent_name=None, name=op_name or None, attributes=attrs,
        )
        # auto-ensure voice-ai is present (presence-idempotent) — see docstring (#42)
        threading.Thread(
            target=_ensure_agent_dispatched, args=(room, "voice-ai"), daemon=True
        ).start()
        return TokenResponse(token=token, url=livekit_url, room=room, identity=identity)
    return _mint(room, identity, invite, ttl_seconds)


# ----- host-call: start a fresh call without an invite (#20) --------------
# The invite gate (#52 fix) protects EXISTING rooms — a leaked slug must not let
# strangers publish into your room. Starting a NEW room is different: there is no
# room to protect yet. So the server generates the slug itself (the caller cannot
# target an existing room → no hijack) and mints directly. Quota-abuse throttling
# is a stopgap per-IP limit here; the real gate is the free-tier wallet (#17-19).

_HOST_HITS: dict[str, list[float]] = {}
_HOST_LIMIT = 5          # calls
_HOST_WINDOW = 600       # seconds (per IP)


def _client_ip(request: Request) -> str:
    """Client-IP hinter Caddy. Das LETZTE X-Forwarded-For-Element hat der nächste
    Proxy (Caddy) selbst gesetzt; frühere Elemente kann der Client fälschen.
    Caddy ohne trusted_proxies (infra/caddy/Caddyfile.tmpl) verwirft eingehende
    X-Forwarded-For-Werte ohnehin, das letzte Element bleibt aber auch dann richtig,
    wenn dort später trusted_proxies gesetzt wird (Review 01.10. #4)."""
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


@app.get("/api/free/remaining")
def free_remaining(request: Request) -> dict:
    """Übrige Gratis-Sekunden heute je Modus für diesen Browser (X-Anon-Id) und
    diese IP, gezählt wie bei host-call / live-room (das knappere Merkmal gilt).
    Nur lesen, nie buchen. Modus aus (Limit 0) -> enabled false, Sekunden 0."""
    keys = freetier.identity_keys(request.headers.get(freetier.ANON_HEADER), _client_ip(request))
    out: dict = {"enabled": {}}
    for mode in ("live", "normal"):
        on = freetier.enabled(mode)
        out["enabled"][mode] = on
        out[f"{mode}_s"] = max(0, int(freetier.remaining_seconds(keys, mode))) if on else 0
    return out


def _host_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _HOST_HITS.get(ip, []) if now - t < _HOST_WINDOW]
    if len(hits) >= _HOST_LIMIT:
        _HOST_HITS[ip] = hits
        return False
    hits.append(now)
    _HOST_HITS[ip] = hits
    return True


class HostCallRequest(BaseModel):
    identity: str = Field(..., min_length=1, max_length=200)
    ttl_seconds: int = Field(3600, ge=60, le=86400)


@app.post("/api/host-call", response_model=TokenResponse)
def host_call(req: HostCallRequest, request: Request) -> TokenResponse:
    """Start a fresh call. Server-generated room (no hijack), direct mint +
    voice-ai dispatch. Stopgap per-IP rate limit until free-tier gate (#17-19)."""
    wallet = billing_routes.wallet_for_call(request, "normal")  # Gating an: 402 ohne Guthaben
    ip = _client_ip(request)
    free_keys = billing_routes.free_keys_for_call(request, "normal", ip, wallet)  # 402 free_limit
    if not _host_rate_ok(ip):
        raise HTTPException(status_code=429, detail="rate limited — try again later")
    room = gen_slug()
    if wallet:  # vor dem Dispatch binden, damit der Worker das Konto sofort sieht
        billing_routes.db.bind_room(room, wallet, "normal", req.ttl_seconds)
    elif free_keys is not None:  # Gratis-Raum: Worker zählt die Minuten auf diese Merkmale
        freetier.register_room(room, "normal", free_keys)
    return _issue(room, req.identity, req.ttl_seconds)


class LiveRoomRequest(BaseModel):
    ttl_seconds: int = Field(3600, ge=300, le=86400)


class LiveRoomResponse(BaseModel):
    room: str
    url: str
    expires_in: int


@app.post("/api/admin/live-room", response_model=LiveRoomResponse)
def admin_live_room(req: LiveRoomRequest, request: Request) -> LiveRoomResponse:
    """Gemini-Live-Testraum anlegen (nur Admin). Schlüssel als Bearer-Header."""
    if not os.environ.get("VOICEHOOK_LIVE_KEY"):
        raise HTTPException(status_code=404, detail="not found")
    auth = request.headers.get("authorization", "")
    given = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not _live_key_ok(given):
        raise HTTPException(status_code=401, detail="unauthorized")
    if budget.exhausted():
        raise HTTPException(status_code=402, detail="live budget for this month is used up")
    room = gen_slug()
    _set_room_agent(room, LIVE_AGENT_NAME, req.ttl_seconds)
    # Bekannt, aber nicht gezählt: der Live-Worker lehnt unbekannte Räume ab (fail-closed).
    freetier.register_room(room, "live", [], exempt=True)
    invite = mint_invite(room, req.ttl_seconds)
    base = os.environ.get("VOICEHOOK_PUBLIC_URL", "https://voicehook.ai").rstrip("/")
    logger.info("[admin] live room=%s -> %s (ttl %ss)", room, LIVE_AGENT_NAME, req.ttl_seconds)
    return LiveRoomResponse(room=room, url=f"{base}/r/{room}?invite={invite}", expires_in=req.ttl_seconds)


# ----- Live-Modus öffentlich (Demo, Oliver 30.09.) -------------------------
# "testweise für alle verfügbar ... für Demo erst mal für alle frei": jeder darf
# ohne Schlüssel einen Live-Raum starten. Schutz bis Login+Guthaben kommen:
# dasselbe IP-Ratenlimit wie /api/host-call (gemeinsamer Zähler) und die
# Monatsbudget-Sperre (budget.exhausted() -> 402). Schalter VOICEHOOK_LIVE_PUBLIC
# (Default an; 0/false/off/no = aus -> 404). Der Admin-Endpunkt bleibt davon unberührt.
# Nach außen gehen nie Beträge, nur verfügbar ja/nein.

def _live_public_on() -> bool:
    v = os.environ.get("VOICEHOOK_LIVE_PUBLIC", "1").strip().lower()
    return v not in {"0", "false", "off", "no", "aus"}


def _live_configured() -> bool:
    """Server hat alles, was ein Live-Raum braucht: Worker-Name, LiveKit-Zugang und
    einen Google-Zugang für Gemini Live (Worker liest dieselbe /opt/voicehook/.env).
    Ob der Dienst voice-ai-live gerade läuft, sieht der HTTP-Server nicht."""
    return bool(
        LIVE_AGENT_NAME.strip()
        and os.environ.get("LIVEKIT_API_KEY")
        and os.environ.get("LIVEKIT_API_SECRET")
        and (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    )


def _live_available() -> bool:
    return _live_public_on() and _live_configured() and not budget.exhausted()


@app.get("/api/live/status")
def live_status(request: Request) -> dict[str, bool]:
    """Darf die Oberfläche den Live-Schalter anbieten? Immer 200, nie Beträge.
    Mit gedecktem Wallet (X-Wallet-Token) zählt das Monatsbudget nicht."""
    if _live_public_on() and _live_configured() and _paying_wallet(request):
        return {"available": True}
    return {"available": _live_available()}


def _paying_wallet(request: Request) -> str | None:
    acc = billing_routes.db.account_for_token(billing_routes.wallet_token(request))
    return acc if acc is not None and billing_routes.db.balance_ueur(acc) > 0 else None


class PublicLiveRoomResponse(TokenResponse):
    invite_url: str
    expires_in: int
    agent: str


@app.post("/api/live-room", response_model=PublicLiveRoomResponse)
def public_live_room(req: HostCallRequest, request: Request) -> PublicLiveRoomResponse:
    """Wie /api/host-call, aber der neue Raum gehört dem Live-Worker. Antwort =
    host-call-Format (token/url/room/identity) plus Einladungslink für Mitspieler."""
    if not _live_public_on():
        raise HTTPException(status_code=404, detail="not found")
    if not _live_configured():
        raise HTTPException(status_code=503, detail="live mode not available")
    wallet = billing_routes.wallet_for_call(request, "live")  # Gating an: 402 ohne Guthaben
    # Monatsbudget gilt nur für Gratis/Demo; zahlende Kunden scheitern nie daran (Review #7).
    if not wallet and budget.exhausted():
        raise HTTPException(status_code=402, detail="live mode is used up for this month")
    ip = _client_ip(request)
    free_keys = billing_routes.free_keys_for_call(request, "live", ip, wallet)  # 402 free_limit
    if not _host_rate_ok(ip):
        raise HTTPException(status_code=429, detail="rate limited, try again later")
    room = gen_slug()
    _set_room_agent(room, LIVE_AGENT_NAME, req.ttl_seconds)
    if wallet:
        billing_routes.db.bind_room(room, wallet, "live", req.ttl_seconds)
    elif free_keys is not None:
        freetier.register_room(room, "live", free_keys)
    tok = _issue(room, req.identity, req.ttl_seconds)  # _agent_for -> Live-Worker
    invite = mint_invite(room, req.ttl_seconds)
    base = os.environ.get("VOICEHOOK_PUBLIC_URL", "https://voicehook.ai").rstrip("/")
    logger.info("[live] public room=%s -> %s (ttl %ss)", room, LIVE_AGENT_NAME, req.ttl_seconds)
    return PublicLiveRoomResponse(
        **tok.model_dump(), invite_url=f"{base}/r/{room}?invite={invite}",
        expires_in=req.ttl_seconds, agent=LIVE_AGENT_NAME,
    )
