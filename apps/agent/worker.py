"""LiveKit worker — joins dispatched rooms as `voice-ai`.

PR-5 wires the full mouthpiece: connect → start AgentSession(stt+tts+llm) →
bind operator.* data-channel handlers → RelayAgent.on_user_turn_completed
raises StopResponse so the model never produces a turn on its own.

Run locally:
    python -m agent.worker dev   # connects to LIVEKIT_URL with API creds
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
    room_io,
)

from . import budget, freetier, procctl
from .billing import db as billing_db
from .billing import pricing as billing_pricing
from .llm import build_llm
from .relay import (
    DEFAULT_PERSONA,
    LOW_BALANCE_ANNOUNCEMENT,
    RelayAgent,
    build_relay_handlers,
    publish_notice,
    speak_notice,
    topic_dispatch,
)
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
WALLET_EMPTY_ANNOUNCEMENT = "Dein Guthaben ist aufgebraucht. Ich beende den Call."
FREE_LIMIT_ANNOUNCEMENT = "Dein Gratis-Verbrauch für heute ist um. Lade Guthaben auf."
DEFAULT_FREE_TICK_SECONDS = 5.0  # Prüftakt der Restzeit-Warnung (LowBalanceWatch)
# Vorwarnung (Oliver 01.10.): reichen Gratis-Rest + Guthaben bei aktuellem Verbrauch
# noch höchstens so lange, einmal pro Call operator.notice + Ansage.
DEFAULT_LOW_BALANCE_WARN_SECONDS = 300.0
DEFAULT_BURN_WINDOW_SECONDS = 180.0     # gleitender Verbrauch der letzten 3 Minuten
DEFAULT_BURN_MIN_SPAN_SECONDS = 60.0    # erst ab 1 min Beobachtung hochrechnen
# Every teardown step is bounded so the teardown itself can never hang.
TEARDOWN_STEP_TIMEOUT = 10.0

# Non-human identities — mirrors vhPresenceSlotFor() in web/voice.html:
# voice-ai worker (`voice-ai`, `agent-AJ_*`) and senior CLIs (claude-*, hermes-*, …).
_NON_HUMAN_IDENTITY = re.compile(
    r"^(voice-ai|agent-AJ_)"
    r"|^(claude|hermes|cursor|openclaw|zeroclaw|codex|gemini|gpt|grok|llama|qwen|deepseek|senior|agent-cli)-",
    re.IGNORECASE,
)


def is_operator_agent(participant: Any) -> bool:
    """Externer Agent (Operator): der Server markiert dessen Token mit vh.role=agent
    (server.py, invite=1-Zweig von /token)."""
    attrs = getattr(participant, "attributes", None) or {}
    return attrs.get("vh.role") == "agent"


def operator_agent_present(room: Any) -> bool:
    return any(is_operator_agent(p) for p in room.remote_participants.values())


def operator_agent_name(room: Any) -> str | None:
    """Sprechbarer Name des zuletzt beigetretenen Agenten (vh.name, sonst LK-Name).

    remote_participants hält die Join-Reihenfolge, der letzte Agent gewinnt. Ohne
    aussprechbaren Namen None, die Prompts sagen dann "dein Agent".
    """
    from .guide import agent_display_name

    agents = [p for p in room.remote_participants.values() if is_operator_agent(p)]
    if not agents:
        return None
    last = agents[-1]
    attrs = getattr(last, "attributes", None) or {}
    return agent_display_name(attrs.get("vh.name") or getattr(last, "name", None) or "")


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
        on_end: Callable[[], Any] | None = None,
    ) -> None:
        self._live = live
        self._on_end = on_end  # z. B. Wallet-Bindung schließen; läuft vor shutdown, nie blockierend
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

    @property
    def ended(self) -> bool:
        return self._ended

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
            if self._on_end is not None:
                try:
                    self._on_end()  # kurzer SQLite-Schreibzugriff, Fehler nie bis shutdown durchreichen
                except Exception as e:  # noqa: BLE001
                    logger.warning("[call_guard] on_end failed: %r", e)
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


class WalletCharger:
    """Bucht echten Verbrauch x Faktor (+ MwSt) vom Wallet, das am Raum hängt.

    Die Zuordnung Raum -> Konto legt der HTTP-Server an (nur host-call / live-room,
    VOR dem Dispatch) in derselben SQLite-Datei; ohne Zuordnung bucht der Charger
    nichts (Call wie bisher). Ist die Zuordnung beendet (Call-Ende) oder abgelaufen
    (TTL), gilt der Raum als `refused`: der Worker lehnt ihn ab, nie läuft ein Call
    auf Kosten des Kontos weiter (Review 01.10. #1). Die Zuordnung wird genau EINMAL je Job gelesen: ein Raum
    ohne Wallet öffnet danach bei keinem Kostenereignis mehr die Datenbank.
    charge() liefert genau EINMAL True, sobald der Saldo <= 0 ist; ein Buchungsfehler
    bei gebundenem Konto zählt als leer (fail-closed).
    """

    def __init__(self, room: str, mode: str) -> None:
        self.room = room
        self.mode = mode  # "normal" | "live" -> Faktor 3 | 1,5
        self.account: str | None = None
        self.exhausted = False
        self.refused = False
        self._looked_up = False

    def lookup(self) -> str | None:
        if not self._looked_up:
            self._looked_up = True
            try:
                bound = billing_db.room_binding(self.room)
            except Exception as e:  # noqa: BLE001
                logger.error("[wallet] lookup room=%s failed: %s", self.room, e)
                bound = None
            if bound and bound[2] == "active":
                self.account = bound[0]
            elif bound:
                self.refused = True  # Bindung beendet/abgelaufen: nicht auf dessen Kosten
        return self.account

    def close(self) -> None:
        """Call-Ende: Bindung schließen (danach kein neuer Call auf Kosten des Kontos)."""
        if self.account is None:
            return
        try:
            billing_db.close_room(self.room)
        except Exception as e:  # noqa: BLE001
            logger.error("[wallet] close room=%s failed: %s", self.room, e)

    def is_empty(self) -> bool:
        acc = self.lookup()
        if acc is None:
            return False
        try:
            return billing_db.balance_ueur(acc) <= 0
        except Exception as e:  # noqa: BLE001
            logger.error("[wallet] balance account=%s failed: %s", acc, e)
            return True

    def charge(self, usd: float) -> bool:
        if usd <= 0:
            return False
        return self.charge_ueur(billing_pricing.charge_ueur(usd, self.mode), usd)

    def charge_ueur(self, ueur: int, usd: float) -> bool:
        """Brutto-Betrag `ueur` abbuchen (`usd` = zugehörige Anbieterkosten fürs Ledger)."""
        acc = self.lookup()
        if acc is None or ueur <= 0 or self.exhausted:
            return False
        try:
            left = billing_db.charge(acc, ueur, room=self.room, mode=self.mode, usd=usd)
        except Exception as e:  # noqa: BLE001
            logger.error("[wallet] charge account=%s failed: %s", acc, e)
            left = 0
        if left <= 0:
            self.exhausted = True
            return True
        return False


def free_tick_seconds() -> float:
    return _positive_env_seconds("VH_FREE_TICK_SECONDS", DEFAULT_FREE_TICK_SECONDS)


class FreeBudget:
    """Gratis-Topf (freetier.py, VH_FREE_EUR_PER_DAY pro UTC-Tag) eines Raums, VOR dem Guthaben.

    Liest die Merkmale des Raum-Erstellers EINMAL beim Start. Gebucht wird NUR aus
    echten Kostenereignissen (settle_cost, aus metrics_collected), nie aus Zeit:
    Stille ohne Kosten zählt nichts herunter (Bug Oliver 01.10.). Beim Start wird der
    Rest einmal gelesen; ist er schon 0, gilt dasselbe wie beim Leerwerden im Call:
      - Raum hat ein Wallet mit Saldo > 0: Gratis-Teil vorbei (`done`), ab jetzt
        bucht der WalletCharger, der Call läuft weiter;
      - sonst kurze Ansage + Call-Ende (Grund free_limit).
    Lese-/Schreibfehler der Gratis-Datenbank zählen als leer (fail-closed).
    """

    def __init__(self, room: str, mode: str, *, wallet: WalletCharger | None = None) -> None:
        self.room = room
        self.mode = mode
        self.keys: list[str] = []
        self._task: asyncio.Task | None = None
        self._wallet = wallet
        self.known = False  # Raum steht in free_rooms (gezählt oder Admin-Ausnahme)
        self.exempt = False  # Admin-/Operator-Raum (free_rooms exempt): darf Rohkosten sehen
        self.counting = False  # Gratis-Teil läuft (geladen und noch nicht aufgebraucht)
        self.done = False      # Gratis-Teil aufgebraucht, Wallet zahlt weiter
        self.left_ueur: int | None = None  # zuletzt bekannter Gratis-Rest (µEUR)
        self.unlimited = False  # Merkmal in VH_FREE_EXEMPT_KEYS: nie leer, nichts gebucht
        self.owner = False  # Raum-Merkmal in VH_FREE_EXEMPT_KEYS (Owner): keine Guthaben-Hinweise
        self._looked_up = False
        self._found: tuple[str, list[str]] | None = None

    def load(self) -> bool:
        """True = Gratis-Raum mit aktiver Prüfung, wird gezählt. `known` sagt, ob der
        Raum überhaupt als Gratis-/Admin-Raum angelegt wurde (Lesefehler -> unbekannt),
        `exempt`, ob er ein Admin-/Operator-Raum ist (auch bei Gratis aus gelesen)."""
        found = self.lookup()
        if not freetier.enabled(self.mode) or not found or not found[1]:
            return False
        self.keys = found[1]
        self.counting = True
        self.unlimited = freetier.is_exempt(self.keys)  # VH_FREE_EXEMPT_KEYS
        return True

    def lookup(self) -> tuple[str, list[str]] | None:
        """free_rooms-Eintrag EINMAL lesen; setzt known/exempt (Lesefehler -> unbekannt)."""
        if self._looked_up:
            return self._found
        self._looked_up = True
        try:
            self._found = freetier.room_keys(self.room)
        except Exception as e:  # noqa: BLE001
            logger.error("[free] lookup room=%s failed: %s", self.room, e)
            self._found = None
        self.known = self._found is not None
        self.exempt = self._found is not None and not self._found[1]
        # Owner (Oliver 02.10.: "Guthaben-Gedöns raus bei mir"): gilt auch, wenn der
        # Gratis-Teil nicht zählt (Live-Monatsbudget weg, Wallet zahlt).
        self.owner = self._found is not None and freetier.is_exempt(self._found[1])
        return self._found

    def start(self, guard: CallGuard) -> None:
        self._task = asyncio.create_task(self._check_start(guard))

    async def _check_start(self, guard: CallGuard) -> None:
        try:
            left = await asyncio.to_thread(freetier.remaining_ueur, self.keys)
        except Exception as e:  # noqa: BLE001
            logger.error("[free] read room=%s failed: %s", self.room, e)
            left = 0  # fail-closed: Gratis-Raum ohne lesbaren Topf
        if not self.counting or guard.ended:
            return  # inzwischen schon per Kostenereignis entschieden
        if self.left_ueur is None:
            self.left_ueur = left
        if left > 0 or self.unlimited:
            return
        self.counting = False
        wallet = self._wallet
        if wallet is not None and wallet.account is not None and not (
            await asyncio.to_thread(wallet.is_empty)
        ):
            logger.info("[free] daily free budget used up in room=%s, wallet continues", self.room)
            self.done = True
            return
        logger.warning("[free] daily free budget used up in room=%s, ending call", self.room)
        await guard.end("free_limit", delete_room=True, announce=FREE_LIMIT_ANNOUNCEMENT)

    def take(self, ueur: int, real_ueur: int | None = None) -> int:
        """`ueur` (Kundenpreis) aus dem Topf nehmen; liefert den Überhang (nicht gedeckt,
        ans Wallet). `real_ueur` = echte Kosten desselben Ereignisses für den internen
        Monatsdeckel (freetier, Modul-Doku). Wird der Topf dabei leer, endet `counting`."""
        if not self.counting or ueur <= 0:
            return max(0, ueur)
        try:
            taken, left = freetier.consume_ueur(self.keys, ueur, real_ueur=real_ueur)
        except Exception as e:  # noqa: BLE001
            logger.error("[free] book room=%s failed: %s", self.room, e)
            taken, left = 0, 0  # fail-closed
        self.left_ueur = left
        if left <= 0 and not self.unlimited:
            self.counting = False
        return ueur - taken


def settle_cost(free: FreeBudget | None, wallet: WalletCharger, usd: float) -> str | None:
    """Ein Kostenereignis verbuchen: erst Gratis-Topf, Überhang ans Wallet
    (Kundenpreis = billing_pricing.charge_ueur, wie der WalletCharger).
    Liefert den Grund fürs Call-Ende ("free_limit" | "wallet_empty") oder None."""
    if usd <= 0:
        return None
    if free is not None and free.counting:
        ueur = billing_pricing.charge_ueur(usd, wallet.mode)
        over = free.take(ueur, billing_pricing.real_ueur(usd))
        if free.counting:
            return None                    # ganz aus dem Gratis-Topf bezahlt
        # Topf ist mit diesem Ereignis leer geworden.
        if wallet.account is None or wallet.exhausted or wallet.is_empty():
            return "free_limit"
        free.done = True                   # ab jetzt zahlt das Wallet
        if over > 0 and wallet.charge_ueur(over, usd * over / ueur):
            return "wallet_empty"
        return None
    if wallet.charge(usd):
        return "wallet_empty"
    return None


class LowBalanceWatch:
    """Einmal pro Call warnen, wenn Gratis-Rest + Guthaben bei aktuellem Verbrauch
    höchstens noch `warn_s` (Default 5 min) reichen.

    Restzeit = (Gratis-Rest + Guthaben) / Verbrauch, alles in µEUR. Gratis-Rest =
    FreeBudget.left_ueur, solange der Gratis-Teil läuft. Verbrauch = gleitender
    Brutto-Preis (echte Kosten x Faktor + MwSt, billing/pricing.py) der letzten
    `window_s` (3 min), erst ab `min_span_s` Beobachtung. Ohne Wallet zählt nur der Gratis-Rest; ein Raum ohne
    Gratis-Zählung und ohne Wallet wird nie gewarnt (nichts kann leer werden).
    Bei der Warnung: on_warn(Restsekunden, Payload) einmal.
    """

    def __init__(
        self,
        free: FreeBudget | None,
        wallet: WalletCharger,
        *,
        warn_s: float = DEFAULT_LOW_BALANCE_WARN_SECONDS,
        window_s: float = DEFAULT_BURN_WINDOW_SECONDS,
        min_span_s: float = DEFAULT_BURN_MIN_SPAN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.free = free
        self.wallet = wallet
        self.warn_s = warn_s
        self.window_s = window_s
        self.min_span_s = min_span_s
        self._clock = clock
        self._started = clock()
        self._costs: list[tuple[float, int]] = []
        self.warned = False
        self._task: asyncio.Task | None = None

    def add_cost(self, usd: float) -> None:
        if usd <= 0:
            return
        now = self._clock()
        self._costs.append((now, billing_pricing.charge_ueur(usd, self.wallet.mode)))
        cutoff = now - self.window_s
        while self._costs and self._costs[0][0] < cutoff:
            self._costs.pop(0)

    def burn_ueur_per_s(self) -> float | None:
        """Brutto-Verbrauch je Sekunde über das Fenster, None = noch zu kurz beobachtet."""
        now = self._clock()
        span = min(self.window_s, now - self._started)
        if span < self.min_span_s:
            return None
        cutoff = now - self.window_s
        return sum(u for t, u in self._costs if t >= cutoff) / span

    def _free_ueur(self) -> int | None:
        if self.free is None or not (self.free.counting or self.free.done):
            return None
        if self.free.done:
            return 0
        return self.free.left_ueur

    def seconds_left(self) -> float | None:
        """Geschätzte Restzeit in s; None = unbekannt oder unbegrenzt."""
        free_u = self._free_ueur()
        if self.free is not None and self.free.counting and free_u is None:
            return None  # Gratis-Rest noch nicht gelesen
        acc = self.wallet.account
        if acc is None and free_u is None:
            return None
        bal = 0
        if acc is not None:
            try:
                bal = billing_db.balance_ueur(acc)
            except Exception as e:  # noqa: BLE001
                logger.error("[low-balance] balance account=%s failed: %s", acc, e)
                return None
        rate = self.burn_ueur_per_s()
        if not rate:
            return None  # Verbrauch noch unbekannt: lieber spät als falsch warnen
        return ((free_u or 0) + max(0, bal)) / rate

    def payload(self, left: float) -> dict:
        free_u = self._free_ueur()
        bal = None
        if self.wallet.account is not None:
            with contextlib.suppress(Exception):
                bal = billing_pricing.ueur_to_eur(billing_db.balance_ueur(self.wallet.account))
        rate = self.burn_ueur_per_s()
        free_s = None
        if free_u is not None:
            free_s = 0 if free_u <= 0 else (int(free_u / rate) if rate else None)
        base = os.environ.get("VOICEHOOK_PUBLIC_URL", "https://voicehook.ai").rstrip("/")
        return {
            "kind": "low_balance",
            "minutes_left": max(0, math.ceil(left / 60)),
            "seconds_left": max(0, int(left)),
            "free_s": free_s,
            "free_eur": None if free_u is None else round(free_u / billing_pricing.UEUR_PER_EUR, 2),
            "balance_eur": bal,
            "topup_url": f"{base}/aufladen",
            "text": LOW_BALANCE_ANNOUNCEMENT,
        }

    async def check(self) -> dict | None:
        """Einmal prüfen; liefert die Payload, wenn jetzt gewarnt werden muss."""
        if self.warned:
            return None
        left = await asyncio.to_thread(self.seconds_left)
        if left is None or left > self.warn_s:
            return None
        self.warned = True
        return await asyncio.to_thread(self.payload, left)

    def start(self, guard: CallGuard, on_warn: Callable[[dict], Any]) -> None:
        async def _run() -> None:
            while not guard.ended and not self.warned:
                await asyncio.sleep(free_tick_seconds())
                if guard.ended:
                    return
                try:
                    p = await self.check()
                except Exception as e:  # noqa: BLE001
                    logger.warning("[low-balance] check failed: %r", e)
                    continue
                if p is not None:
                    logger.info("[low-balance] room=%s %s", self.wallet.room, json.dumps(p))
                    await on_warn(p)

        self._task = asyncio.create_task(_run())


def low_balance_warn_seconds() -> float:
    return _positive_env_seconds("VH_LOW_BALANCE_WARN_SECONDS", DEFAULT_LOW_BALANCE_WARN_SECONDS)


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
    mode = "live" if live_mode else "normal"
    wallet = WalletCharger(ctx.room.name, mode)
    paid = wallet.lookup() is not None
    if wallet.refused:
        # Bindung beendet oder abgelaufen: der Raum läuft nie wieder auf Kosten des Kontos.
        logger.warning("[wallet] binding closed/expired, refusing room=%s", ctx.room.name)
        ctx.shutdown(reason="wallet_binding_closed")
        return
    if live_mode and not paid and budget.exhausted():
        # Monatsbudget (nur Gratis/Demo) weg: keine kostenpflichtige Live-Session öffnen.
        logger.warning("[live-budget] exhausted (%.2f USD), refusing room=%s", budget.spent_usd(), ctx.room.name)
        ctx.shutdown(reason="live_budget_exhausted")
        return
    # Gratis-Kontingent: Räume, die der HTTP-Server als Gratis-Raum angelegt hat
    # (host-call / live-room / invite-room). Hat der Raum zusätzlich ein Wallet, zahlt
    # es erst, wenn der Gratis-Teil aufgebraucht ist (Oliver 01.10.).
    free = FreeBudget(ctx.room.name, mode, wallet=wallet)
    if live_mode and paid and budget.exhausted():
        # Gratis-Live-Verbrauch kommt aus dem Monatsbudget; ist es weg, zahlt das
        # Wallet von Anfang an (PR #93 low). Raum bleibt bekannt (paid).
        logger.info("[live-budget] exhausted, room=%s runs on wallet only", ctx.room.name)
        counted = False
    else:
        counted = free.load()
    if not counted and wallet.is_empty():
        # Raum gehört einem Wallet ohne Guthaben und hat keinen Gratis-Teil (mehr):
        # keine kostenpflichtige Session öffnen. Mit Gratis-Teil endet der Call erst,
        # wenn der aufgebraucht ist (FreeBudget).
        logger.warning("[wallet] empty, refusing room=%s", ctx.room.name)
        ctx.shutdown(reason="wallet_empty")
        return
    if not paid and freetier.enabled(mode) and not free.known:
        # fail-closed (Review 01.10. #2, Normal seit PR #93): Raum ohne Wallet und ohne
        # Gratis-Eintrag (z. B. neuer Slug über /api/token?invite=1, aufgeräumt, nie
        # über host-call/invite-room/live-room angelegt) läuft nicht unbegrenzt.
        logger.warning("[free] %s room=%s has neither wallet nor free entry, refusing", mode, ctx.room.name)
        ctx.shutdown(reason="free_room_unknown")
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

    # Werksrolle (voicehook-Guide) automatisch aus, sobald ein externer Agent im Raum
    # ist, auch ohne Persona-Push; geht der letzte Agent, ist sie wieder an.
    def _sync_role(*_args) -> None:  # noqa: ANN002
        asyncio.create_task(handlers.on_agent_presence(
            operator_agent_present(ctx.room), operator_agent_name(ctx.room)))

    for _ev in ("participant_connected", "participant_disconnected", "participant_attributes_changed"):
        ctx.room.on(_ev, _sync_role)

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
        # Nachfrage nach dem Stand des Agenten -> operator.status_request (board.py)
        asyncio.create_task(handlers.on_user_text(text))

    # Laufende Kosten (beide Modi) -> Log + Topic `cost` für die Anzeige im Browser.
    # Gesendet wird der KUNDENPREIS in Euro (echte Kosten x Faktor + MwSt, dieselbe
    # Rechnung wie Wallet und Gratis-Topf: billing_pricing.charge_ueur je Ereignis).
    # Rohkosten in USD und ihre Basis nur in Admin-/Operator-Räumen (free_rooms exempt),
    # sonst wäre die Marge über den Datenkanal ablesbar (Oliver 01.10.). Gesendet wird
    # nur, wenn sich die Summe geändert hat (kein Takt, Stille erzeugt keine Meldung).
    from .live import CostMeter

    meter = CostMeter("live" if live_mode else "pipeline")
    free.lookup()  # exempt auch bei Gratis aus / Wallet-Raum kennen (Rohkosten-Anzeige)
    billed_ueur = [0]
    watch = LowBalanceWatch(free if counted else None, wallet, warn_s=low_balance_warn_seconds())
    turns = [0]
    guard_ref: list[CallGuard] = []  # wird unten gesetzt; Budget-Ende braucht den Guard

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:  # noqa: ANN001
        m = getattr(ev, "metrics", None)
        usd = meter.add(m)
        if usd <= 0:
            return
        # Monatsbudget = alles, was nicht vom Guthaben bezahlt wird (Review #7): Gratis/
        # Demo-Räume ganz, Räume mit Wallet nur ihr Gratis-Teil (PR #93 low). Beenden
        # nur ohne Wallet; mit Wallet zählt der Gratis-Teil, und ein schon erschöpftes
        # Budget lässt den Gratis-Teil beim Start ganz weg (siehe oben).
        if live_mode and (not paid or free.counting):
            month = budget.add_usd(usd)
            if not paid and month >= budget.limit_usd() and guard_ref:
                logger.warning("[live-budget] reached %.4f USD in room=%s, ending call", month, ctx.room.name)
                asyncio.create_task(
                    guard_ref[0].end("live_budget", delete_room=False, announce=LIVE_BUDGET_ANNOUNCEMENT)
                )
        watch.add_cost(usd)
        # Erst Gratis-Topf, Überhang ans Wallet (nur echte Kosten, nie Zeit).
        end_reason = settle_cost(free if counted else None, wallet, usd)
        if end_reason == "free_limit" and guard_ref:
            logger.warning("[free] daily free budget used up in room=%s, ending call", ctx.room.name)
            asyncio.create_task(
                guard_ref[0].end("free_limit", delete_room=True, announce=FREE_LIMIT_ANNOUNCEMENT)
            )
        elif end_reason == "wallet_empty" and guard_ref:
            logger.warning("[wallet] empty in room=%s, ending call", ctx.room.name)
            asyncio.create_task(
                guard_ref[0].end("wallet_empty", delete_room=False, announce=WALLET_EMPTY_ANNOUNCEMENT)
            )
        if type(m).__name__ == "RealtimeModelMetrics":
            turns[0] += 1
            logger.info(
                "[live-cost] room=%s turn=%d in=%d out=%d usd=%.5f total_usd=%.4f",
                ctx.room.name, turns[0], getattr(m, "input_tokens", 0),
                getattr(m, "output_tokens", 0), usd, meter.usd,
            )
        billed_ueur[0] += billing_pricing.charge_ueur(usd, mode)
        update = meter.take_update()
        if update is None:
            return
        out = {"eur": round(billed_ueur[0] / billing_pricing.UEUR_PER_EUR, 4), "mode": update["mode"]}
        if free.exempt:
            out.update(usd=update["usd"], basis=update["basis"], prices_as_of=update["prices_as_of"])
        payload = json.dumps(out).encode()

        async def _send_cost() -> None:
            try:
                await ctx.room.local_participant.publish_data(payload=payload, topic="cost")
            except Exception as e:  # noqa: BLE001
                logger.debug("[cost publish] %s", e)
        asyncio.create_task(_send_cost())

    # Operator-Text ab Sprechbeginn (UI-Paket 6): Sobald die Wiedergabe einer
    # Operator-Ausgabe startet (agent_state -> speaking, erster Audio-Frame), geht
    # der volle Text auf `transcript.live` (phase=start), am Ende phase=end mit
    # interrupted. `transcript` bleibt unverändert "tatsächlich gesprochen".
    from .relay import TOPIC_TRANSCRIPT_LIVE

    live_sent: set[str] = set()

    def _pub_live(obj: dict) -> None:
        payload = json.dumps(obj).encode()

        async def _send() -> None:
            try:
                await ctx.room.local_participant.publish_data(payload=payload, topic=TOPIC_TRANSCRIPT_LIVE)
            except Exception as e:  # noqa: BLE001
                logger.debug("[transcript.live publish] %s", e)
        asyncio.create_task(_send())

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:  # noqa: ANN001
        if getattr(ev, "new_state", None) != "speaking":
            return
        h = session.current_speech
        text = handlers.operator_text_for(h) if handlers.operator_text_for else None
        sid = str(getattr(h, "id", "") or "")
        if not text or not sid or sid in live_sent:
            return
        live_sent.add(sid)
        _pub_live({"phase": "start", "role": "operator", "id": sid, "text": text})

        def _done(hh) -> None:  # noqa: ANN001
            live_sent.discard(sid)
            _pub_live({"phase": "end", "role": "operator", "id": sid,
                       "interrupted": bool(getattr(hh, "interrupted", False))})
        try:
            h.add_done_callback(_done)
        except Exception as e:  # noqa: BLE001
            logger.debug("[transcript.live done-callback] %s", e)

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
        on_end=wallet.close if paid else None,
    )
    guard_ref.append(guard)
    guard.start()
    if counted:
        free.start(guard)

    async def _warn(p: dict) -> None:
        await publish_notice(ctx.room, p)
        speak_notice(session, LOW_BALANCE_ANNOUNCEMENT, live=live_mode)

    # Owner-Ausnahme (VH_FREE_EXEMPT_KEYS): keine Low-Balance-Warnung, weder Notice
    # noch Ansage (Oliver 02.10.). Live-Monatssperre, Gratis-/Wallet-Ende bleiben.
    if (counted or paid) and not free.owner:
        watch.start(guard, _warn)
    # Explicit room options (don't rely on lib defaults): close the session when
    # the linked participant leaves. Room deletion is owned by CallGuard so it
    # only happens when no human is left (or on the hard cap).
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(close_on_disconnect=True, delete_room_on_close=False),
    )
    # Agent war schon vor voice-ai im Raum (Operator-Join dispatcht voice-ai erst)
    await handlers.on_agent_presence(operator_agent_present(ctx.room), operator_agent_name(ctx.room))


def build_worker_options() -> WorkerOptions:
    """Factory exposed for unit tests. Prod-Optionen (Drain, Last, Idle) aus procctl."""
    return WorkerOptions(entrypoint_fnc=entrypoint, agent_name=AGENT_NAME,
                         **procctl.worker_option_kwargs())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = AgentServer.from_server_options(build_worker_options())
    server.on("worker_registered", procctl.notify_ready)  # Type=notify: READY erst nach Registrierung
    cli.run_app(server)


if __name__ == "__main__":
    main()
