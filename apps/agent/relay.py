"""operator.* data-channel relay — knowledge-transfer mouthpiece.

Design (mode B, voicehook-v3#28 + Wissenstransfer):
- Default: the voice-ai MAY answer simple questions from its persona (the
  pushed knowledge-graph) — RelayAgent does NOT stop the LLM. For anything
  substantive/unknown the persona instructs it to defer to the operator, who
  pushes the verbatim answer via `operator.say`.
- Strict mode: `operator.mode`={"mode":"strict"} (the CLI's --strict-relay)
  flips RelayAgent.strict → on_user_turn_completed raises StopResponse, so the
  LLM never produces a turn on its own (pure mouthpiece).

Topics handled here:
- operator.say        — TTS the text. Ist nichts Ungesprochenes offen: sofort.
                        Sonst: Ausgabe stoppen, ungesprochene Aussagen per
                        operator.revise ans Brain zurück, neue Aussage halten, bis
                        das Brain mit mode "overwrite" die Zusammenfassung schickt
                        (nach HOLD_S spricht die gehaltene). mode "append": anhängen.
- operator.revise     — (Agent -> Operator) {unspoken:[...], new, text:Anweisung}
- operator.persona    — replace the agent's instructions (live-injected knowledge)
- operator.mode       — switch strict/auto generation ({"mode":"strict"|"auto"})
- operator.interrupt  — alles stoppen, ungesprochene Aussagen per operator.revise melden
- operator.inject     — synthetic user-turn (test harness; operator reads transcript)

PR-12 adds: every operator.say also publishes {role:"agent",text:...} on the
`transcript` topic so the browser UI can render it (v3 parity).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from livekit.agents import Agent, StopResponse

if TYPE_CHECKING:
    from livekit.agents import AgentSession
    from livekit.rtc import DataPacket, Room

logger = logging.getLogger("voicehook.relay")

TOPIC_SAY = "operator.say"
TOPIC_PERSONA = "operator.persona"
TOPIC_MODE = "operator.mode"
TOPIC_INTERRUPT = "operator.interrupt"
TOPIC_INJECT = "operator.inject"
TOPIC_REVISE = "operator.revise"   # agent -> operator: ungesprochene Aussagen zurück

HOLD_S = 8.0  # Olli-Regel "Stille ist der Killer, ab 8s ansagen": so lange wartet ein
             # zurückgehaltenes say auf das zusammengefasste overwrite des Brains

DEFAULT_PERSONA = (
    "Du bist die Stimme von voicehook.ai. Du antwortest aus deinem Kontext "
    "(was dir der Operator als Persona/Graph gegeben hat). Simple Fragen "
    "beantwortest du selbst, kurz und praezise. Fuer alles Substantielle, "
    "Technische oder Unbekannte sagst du 'Moment, ich geb das an den Operator' "
    "und wartest auf operator.say. Du erfindest NICHTS."
)


class RelayAgent(Agent):
    """Mouthpiece with two modes:
      - auto (default): the LLM answers simple questions from its persona
        (the pushed knowledge-graph); operator.say overrides substantive content.
      - strict: StopResponse on every turn — the LLM never speaks on its own."""

    def __init__(self, *, instructions: str = "", strict: bool = False, **kwargs) -> None:  # noqa: ANN003
        super().__init__(instructions=instructions, **kwargs)
        self.strict = strict

    async def on_user_turn_completed(self, *args, **kwargs) -> None:  # noqa: D401, ANN001
        if self.strict:
            raise StopResponse()
        # auto mode: fall through → the LLM answers from its persona (knowledge transfer).


@dataclass
class RelayHandlers:
    """Resolved handlers for unit-test introspection."""

    on_say: callable
    on_persona: callable
    on_mode: callable
    on_interrupt: callable
    on_inject: callable


def _decode(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


TOPIC_TRANSCRIPT = "transcript"


def _publish_transcript_safe(room: Room | None, role: str, text: str) -> None:
    """Fire-and-forget publish_data; never let UI feedback break the relay."""
    if room is None or not text:
        return
    payload = json.dumps({"role": role, "text": text}).encode()
    async def _send() -> None:
        try:
            await room.local_participant.publish_data(payload=payload, topic=TOPIC_TRANSCRIPT)
        except Exception as e:  # noqa: BLE001
            logger.warning("[transcript publish] %s", e)
    asyncio.create_task(_send())


def unspoken_rest(full: str, spoken: str) -> str:
    """Was von `full` noch nicht gesprochen ist, gegeben den gesprochenen Anfang.

    Das synchronisierte Transkript kann in Groß/Klein und Interpunktion abweichen,
    daher erst Präfix, dann wortweise.
    """
    full, spoken = (full or "").strip(), (spoken or "").strip()
    if not spoken:
        return full
    if full.startswith(spoken):
        return full[len(spoken):].strip()
    return " ".join(full.split()[len(spoken.split()):])


def _spoken_text(handle: object) -> str:
    try:
        items = list(getattr(handle, "chat_items", None) or [])
    except Exception:  # noqa: BLE001
        return ""
    return " ".join((getattr(i, "text_content", "") or "") for i in items).strip()


def _is_done(handle: object) -> bool:
    try:
        return bool(handle.done())
    except Exception:  # noqa: BLE001
        return False


def build_relay_handlers(
    session: AgentSession,
    agent: RelayAgent,
    *,
    room: Room | None = None,
    hold_s: float = HOLD_S,
    live: bool = False,
) -> RelayHandlers:
    """Build per-topic handler closures bound to a session + agent.

    `room` is optional — when passed, every operator.say also publishes a
    {role:"agent",text:...} packet on the `transcript` topic so the browser UI
    can render the agent turn. (v3 parity, PR-12.)
    """

    # Offene operator.say-Ausgaben: (seq, text, handle). session.say() hängt nur
    # hinten an; ohne Buchführung spricht der Agent Minuten später Aussagen, die
    # der Operator längst revidiert hat, und der Operator weiß nicht, was davon
    # schon gesprochen wurde.
    pending: list[tuple[object, str, object]] = []
    held: dict = {"text": None, "task": None}

    def _speak(text: str, seq: object = None) -> None:
        logger.info("[operator.say]%s %s", " (live)" if live else "", text[:200])
        if live:
            # Realtime-Modell hat kein wörtliches TTS: Operator-Text wird Anweisung.
            # Das tatsächlich Gesprochene publiziert der Worker (conversation_item_added).
            # als markierter User-Turn (role=user); instructions= würde als
            # role="model"-Turn ankommen und Gemini hielte es für eigenes Gerede
            from .live import LIVE_SAY_USER

            handle = session.generate_reply(
                user_input=LIVE_SAY_USER.format(text=text), allow_interruptions=True
            )
        else:
            # Transkript kommt vom Worker (conversation_item_added) mit dem tatsächlich
            # Gesprochenen, auch für Eigenantworten; hier nicht doppelt senden.
            # allow_interruptions=True = full-duplex barge-in: the user can comment
            # while the mouthpiece is speaking and the STT keeps hearing them.
            handle = session.say(text, allow_interruptions=True)
        pending[:] = [p for p in pending if not _is_done(p[2])]
        if handle is not None:
            pending.append((seq, text, handle))

    def _drop_hold() -> None:
        task = held["task"]
        if task is not None and not task.done():
            task.cancel()
        held["text"], held["task"] = None, None

    def _stop_session() -> None:
        # force=True: livekit wirft sonst RuntimeError statt zu stoppen, sobald die
        # laufende Ausgabe keine Unterbrechung erlaubt. Stoppt auch Eigenantworten.
        try:
            session.interrupt(force=True)
        except Exception as e:  # noqa: BLE001 — nichts läuft / Session gestoppt
            logger.debug("[operator.say] session.interrupt: %s", e)

    async def _cancel_open() -> list[str]:
        """Stoppt alle offenen Ausgaben, liefert die ungesprochenen Reste."""
        open_ = [p for p in pending if not _is_done(p[2])]
        pending.clear()
        for _seq, _text, handle in open_:
            try:
                handle.interrupt(force=True)
            except Exception as e:  # noqa: BLE001
                logger.debug("[operator.say] cancel handle: %s", e)
        _stop_session()
        # erst nach dem Abbruch steht fest, was tatsächlich gesprochen wurde
        waits = [h.wait_for_playout() for _s, _t, h in open_ if hasattr(h, "wait_for_playout")]
        if waits:
            # Timeout: dann gilt der gesprochene Stand bis hier
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*waits, return_exceptions=True), timeout=2.0)
        rest = [unspoken_rest(text, _spoken_text(h)) for _s, text, h in open_]
        return [r for r in rest if r]

    async def _ask_revise(unspoken: list[str], new: str) -> None:
        listed = " ".join(f"[{i + 1}] {u}" for i, u in enumerate(unspoken))
        if new:
            instr = (
                f"REVISE: Noch NICHT gesprochen: {listed}. Deine neue Aussage: [neu] {new}. "
                "Fasse alles zu EINER Aussage zusammen: nichts Wichtiges auslassen, "
                "Falsches und Überholtes streichen. Sende sie als operator.say mit "
                f'mode "overwrite". Ohne Antwort in {hold_s:g}s spreche ich [neu].'
            )
        else:
            instr = (
                f"REVISE: Abgebrochen, noch NICHT gesprochen: {listed}. "
                'Falls davon noch etwas gilt: zusammengefasst als operator.say mit mode "overwrite" senden.'
            )
        logger.info("[operator.revise] %d ungesprochen, neu=%s", len(unspoken), bool(new))
        if room is None:
            return
        payload = json.dumps({"unspoken": unspoken, "new": new, "text": instr}, ensure_ascii=False).encode()
        try:
            await room.local_participant.publish_data(payload=payload, topic=TOPIC_REVISE, reliable=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[operator.revise publish] %s", e)

    async def _speak_after_hold(text: str) -> None:
        await asyncio.sleep(hold_s)
        if held["text"] == text:
            held["text"], held["task"] = None, None
            logger.info("[operator.say] kein overwrite in %.1fs, spreche gehaltene Aussage", hold_s)
            _speak(text)

    async def on_say(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        seq = data.get("seq", data.get("_seq"))  # CLI taggt _seq
        mode = (data.get("mode") or "revise").strip().lower()
        if mode == "append":
            _speak(text, seq)
            return
        if mode == "overwrite":
            # Zusammenfassung vom Brain: ersetzt alles Offene und Gehaltene
            _drop_hold()
            await _cancel_open()
            _speak(text, seq)
            return
        # Default revise
        _drop_hold()
        if data.get("priority") == "interrupt":
            _stop_session()          # nur ausdrücklich: laufende Ausgabe abbrechen
        if not any(not _is_done(p[2]) for p in pending):
            # Nichts Eigenes offen: einreihen. Eine laufende Eigenantwort des Agents
            # (auto mode) spricht zu Ende, der Operator fällt ihm nicht ins Wort
            # (Olli 30.09.: "Operator say fällt ihm ins Wort").
            _speak(text, seq)
            return
        unspoken = await _cancel_open()
        if not unspoken:
            _speak(text, seq)
            return
        held["text"] = text
        held["task"] = asyncio.create_task(_speak_after_hold(text))
        await _ask_revise(unspoken, text)

    async def on_persona(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        if live:
            # Realtime: update_instructions wäre ein model-Turn -> markierter User-Turn
            from .live import LIVE_PERSONA_USER

            ctx = agent.chat_ctx.copy()
            ctx.add_message(role="user", content=LIVE_PERSONA_USER.format(text=text))
            await agent.update_chat_ctx(ctx)
            logger.info("[operator.persona] (live) %d chars als User-Turn", len(text))
            return
        await agent.update_instructions(text)
        logger.info("[operator.persona] %d chars injected", len(text))

    async def on_mode(packet: DataPacket) -> None:
        data = _decode(packet.data)
        mode = (data.get("mode") or "").strip().lower()
        agent.strict = mode == "strict"
        logger.info("[operator.mode] strict=%s", agent.strict)

    async def on_interrupt(_packet: DataPacket) -> None:
        _drop_hold()
        unspoken = await _cancel_open()
        logger.info("[operator.interrupt] %d ungesprochen", len(unspoken))
        if unspoken:
            await _ask_revise(unspoken, "")

    async def on_inject(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        # Synthetic turn into the chat-context (NOT spoken). The agent ctx is
        # read-only, so copy → mutate → update_chat_ctx.
        ctx = agent.chat_ctx.copy()
        ctx.add_message(role=data.get("role", "user"), content=text)
        await agent.update_chat_ctx(ctx)
        logger.info("[operator.inject] %s", text[:200])

    return RelayHandlers(
        on_say=on_say, on_persona=on_persona, on_mode=on_mode,
        on_interrupt=on_interrupt, on_inject=on_inject,
    )


def topic_dispatch(handlers: RelayHandlers) -> dict[str, callable]:
    """Map operator.* topic → handler. Used by the worker's data-channel subscription."""
    return {
        TOPIC_SAY: handlers.on_say,
        TOPIC_PERSONA: handlers.on_persona,
        TOPIC_MODE: handlers.on_mode,
        TOPIC_INTERRUPT: handlers.on_interrupt,
        TOPIC_INJECT: handlers.on_inject,
    }
