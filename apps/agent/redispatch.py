"""Delta nach Worker-Neustart zurück in laufende Räume (Oliver 02.10.2026).

Vorfall 02.10. 09:26 UTC: Deploy startete voicehook-agent + voice-ai-live neu, der
Delta-Job im Raum drift-calm-signal-UNVK endete (exit 255), niemand dispatchte neu,
Oliver saß allein im Raum. Ein Dispatch passiert sonst nur bei Token-Ausgabe.

Wächter im HTTP-Hauptdienst: alle REDISPATCH_INTERVAL_S Sekunden ListRooms +
ListParticipants (Box-lokal, twirp). Raum mit Mensch und ohne Delta (kind AGENT) ->
server._dispatch_now() mit dem richtigen Worker (normal/live). Die Zuordnung
Raum -> Worker liegt im Prozessspeicher (_ROOM_AGENT) und ist nach einem Neustart
weg, deshalb zählt die gespeicherte Raum-Art (room_wallets.mode bzw.
free_rooms.mode). Unbekannte Räume (weder Wallet noch Gratis-Eintrag) und beendete
Bindungen werden nie neu besetzt (der Worker lehnt sie ohnehin ab, Kosten).
Höchstens MAX_ATTEMPTS Versuche je Raum im Fenster, dann gibt der Wächter auf und das
Frontend beendet den Call (PR #126: Agent > Karenz weg).
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
import urllib.request
from collections.abc import Callable

logger = logging.getLogger("voicehook.redispatch")

REDISPATCH_INTERVAL_S = 3.0
GRACE_S = 4.0               # so lange muss Delta fehlen (Call-Start: Delta tritt gerade bei)
MIN_SPACING_S = 20.0        # Abstand zwischen zwei Versuchen je Raum
MAX_ATTEMPTS = 3            # je Raum im Fenster ATTEMPT_WINDOW_S, danach aufgeben
ATTEMPT_WINDOW_S = 600.0


def is_worker(p: dict) -> bool:
    """voice-ai/voice-ai-live Worker (LiveKit kind AGENT)."""
    return p.get("kind") in (4, "AGENT")


def is_operator(p: dict) -> bool:
    return (p.get("attributes") or {}).get("vh.role") == "agent"


def is_human(p: dict) -> bool:
    return not is_worker(p) and not is_operator(p)


def needs_delta(participants: list[dict]) -> bool:
    """Mensch im Raum, aber kein Delta-Worker."""
    return any(is_human(p) for p in participants) and not any(is_worker(p) for p in participants)


def room_mode(room: str) -> str | None:
    """'live' | 'normal' aus dem gespeicherten Raum, None = unbekannt/beendet (nie besetzen)."""
    from . import freetier
    from .billing import db as billing_db

    try:
        b = billing_db.room_binding(room)
    except Exception as e:  # noqa: BLE001
        logger.warning("[redispatch] binding %s: %s", room, e)
        b = None
    if b is not None:
        return b[1] if b[2] == "active" else None
    try:
        rk = freetier.room_keys(room)
    except Exception as e:  # noqa: BLE001
        logger.warning("[redispatch] free_rooms %s: %s", room, e)
        rk = None
    return rk[0] if rk is not None else None


def agent_for_mode(mode: str, live_agent: str) -> str:
    return live_agent if mode == "live" else "voice-ai"


class Redispatcher:
    """Ein Durchlauf = scan(); Abhängigkeiten injizierbar (Tests)."""

    def __init__(self, *, list_rooms: Callable[[], list[str]],
                 list_participants: Callable[[str], list[dict]],
                 dispatch: Callable[[str, str], None],
                 mode_of: Callable[[str], str | None] = room_mode,
                 live_agent: str = "voice-ai-live",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.list_rooms, self.list_participants = list_rooms, list_participants
        self.dispatch, self.mode_of, self.live_agent, self.clock = dispatch, mode_of, live_agent, clock
        self.attempts: dict[str, list[float]] = {}
        self.missing_since: dict[str, float] = {}

    def scan(self) -> list[tuple[str, str]]:
        done: list[tuple[str, str]] = []
        now = self.clock()
        rooms = self.list_rooms()
        for k in [r for r in (*self.attempts, *self.missing_since) if r not in rooms]:
            self.attempts.pop(k, None)  # Raum weg: Zähler weg
            self.missing_since.pop(k, None)
        for room in rooms:
            try:
                parts = self.list_participants(room)
            except Exception as e:  # noqa: BLE001
                logger.warning("[redispatch] participants %s: %s", room, e)
                continue
            if not needs_delta(parts):
                self.missing_since.pop(room, None)
                if any(is_worker(p) for p in parts):
                    self.attempts.pop(room, None)  # Delta ist (wieder) da: Zähler zurück
                continue
            since = self.missing_since.setdefault(room, now)
            if now - since < GRACE_S:
                continue
            mode = self.mode_of(room)
            if mode is None:
                continue
            tries = [t for t in self.attempts.get(room, []) if now - t < ATTEMPT_WINDOW_S]
            if len(tries) >= MAX_ATTEMPTS or (tries and now - tries[-1] < MIN_SPACING_S):
                self.attempts[room] = tries
                continue
            agent = agent_for_mode(mode, self.live_agent)
            try:
                self.dispatch(room, agent)
            except Exception as e:  # noqa: BLE001
                logger.warning("[redispatch] dispatch %s: %s", room, e)
            tries.append(now)
            self.attempts[room] = tries
            logger.info("[redispatch] room=%s mensch ohne Delta -> %s (Versuch %d)", room, agent, len(tries))
            done.append((room, agent))
        return done


# ----- Box-lokale LiveKit-Anbindung ---------------------------------------------------
def _jwt(key: str, secret: str, video: dict) -> str:
    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    n = int(time.time())
    h = b64(b'{"alg":"HS256","typ":"JWT"}')
    p = b64(json.dumps({"iss": key, "nbf": n - 5, "exp": n + 60, "video": video}).encode())
    return f"{h}.{p}." + b64(hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest())


def _twirp(method: str, body: dict, video: dict) -> dict:
    key, secret = os.environ.get("LIVEKIT_API_KEY", ""), os.environ.get("LIVEKIT_API_SECRET", "")
    req = urllib.request.Request(
        "http://127.0.0.1:7880/twirp/livekit.RoomService/" + method, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _jwt(key, secret, video)})
    with urllib.request.urlopen(req, timeout=5) as f:
        return json.loads(f.read() or b"{}")


def _lk_rooms() -> list[str]:
    return [r["name"] for r in (_twirp("ListRooms", {}, {"roomList": True}).get("rooms") or [])]


def _lk_participants(room: str) -> list[dict]:
    return _twirp("ListParticipants", {"room": room},
                  {"roomAdmin": True, "room": room}).get("participants") or []


def start_background() -> threading.Thread | None:
    """Im HTTP-Hauptdienst starten (__main__). Aus mit VOICEHOOK_REDISPATCH=0."""
    if os.environ.get("VOICEHOOK_REDISPATCH", "1") == "0":
        return None
    if not (os.environ.get("LIVEKIT_API_KEY") and os.environ.get("LIVEKIT_API_SECRET")):
        return None
    from . import server

    rd = Redispatcher(list_rooms=_lk_rooms, list_participants=_lk_participants,
                      dispatch=server._dispatch_now, live_agent=server.LIVE_AGENT_NAME)

    def _loop() -> None:
        while True:
            try:
                rd.scan()
            except Exception as e:  # noqa: BLE001
                logger.warning("[redispatch] scan: %s", e)
            time.sleep(REDISPATCH_INTERVAL_S)

    t = threading.Thread(target=_loop, daemon=True, name="vh-redispatch")
    t.start()
    return t
