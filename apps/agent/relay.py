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
- operator.notice     (Agent -> alle) Hinweis des Servers, z. B. {kind:"low_balance",
                        minutes_left,...}: Gratis+Guthaben reichen noch ~5 min. Der
                        Worker sendet ihn einmal pro Call und sagt LOW_BALANCE_ANNOUNCEMENT.

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

from .guide import VOICEHOOK_GUIDE
from .speaker import PrimarySpeakerFilter, diarize_enabled

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
TOPIC_NOTICE = "operator.notice"   # agent -> alle: Hinweis (low_balance), Browser + Operator

LOW_BALANCE_ANNOUNCEMENT = "Hey, Achtung, das Guthaben ist in wenigen Minuten leer."

HOLD_S = 8.0  # Olli-Regel "Stille ist der Killer, ab 8s ansagen": so lange wartet ein
             # zurückgehaltenes say auf das zusammengefasste overwrite des Brains

# Neutrale Sprachrohr-Rolle ohne Werksrolle: gilt, sobald ein externer Agent
# (vh.role=agent) im Raum ist und noch keine eigene Persona geschickt hat.
OPERATOR_PERSONA = (
    "Du bist die Stimme von voicehook.ai. Du antwortest aus deinem Kontext "
    "(was dir der Operator als Persona/Graph gegeben hat). Simple Fragen "
    "beantwortest du selbst, kurz und praezise. Fuer alles Substantielle, "
    "Technische oder Unbekannte sagst du 'Moment, ich geb das an den Operator' "
    "und wartest auf operator.say. Du erfindest NICHTS. Fragen nach Faehigkeiten, "
    "Zugriff, ob etwas funktioniert, oder alles, was du annehmen muesstest, "
    "beantwortest du NIE selbst, verneinst und behauptest nichts, sondern sagst nur "
    "'Moment, ich schau nach.' und wartest auf den Operator."
)

DEFAULT_PERSONA = VOICEHOOK_GUIDE + (
    "Du antwortest aus deinem Kontext (dieses voicehook-Wissen oder was dir der "
    "Operator als Persona/Graph gegeben hat). Simple Fragen beantwortest du selbst, "
    "kurz und praezise. Ist ein Operator im Raum, sagst du fuer alles Substantielle, "
    "Technische oder Unbekannte 'Moment, ich geb das an den Operator' und wartest auf "
    "operator.say. Du erfindest NICHTS. Ist ein Operator im Raum, gilt: Fragen nach "
    "Faehigkeiten, Zugriff, ob etwas funktioniert, oder alles, was du annehmen "
    "muesstest, beantwortest du NIE selbst, verneinst und behauptest nichts, sondern "
    "sagst nur 'Moment, ich schau nach.' und wartest auf den Operator."
)


_AUTO = object()  # RelayAgent(speakers=...) nicht angegeben -> Schalter entscheidet


class RelayAgent(Agent):
    """Mouthpiece with two modes:
      - auto (default): the LLM answers simple questions from its persona
        (the pushed knowledge-graph); operator.say overrides substantive content.
      - strict: StopResponse on every turn — the LLM never speaks on its own."""

    def __init__(  # noqa: ANN001, ANN003
        self, *, instructions: str = "", strict: bool = False, gate=None, speakers=_AUTO, **kwargs
    ) -> None:
        super().__init__(instructions=instructions, **kwargs)
        self.strict = strict
        self.gate = gate  # SpeechGate: nur Sprache geht zur (minutenweise bezahlten) STT
        # Hauptsprecher-Filter: nur die Stimme des Hauptsprechers erreicht das LLM.
        # Ohne Angabe entscheidet der Schalter VOICEHOOK_STT_DIARIZE (Default an).
        if speakers is _AUTO:
            speakers = PrimarySpeakerFilter() if diarize_enabled() else None
        self.speakers = speakers

    def stt_node(self, audio, model_settings):  # noqa: ANN001, ANN201
        if self.gate is not None:
            audio = self.gate.filter(audio)
        events = Agent.default.stt_node(self, audio, model_settings)
        if self.speakers is not None:
            return self.speakers.filter(events)
        return events

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
    is_operator_speech: callable = None  # (handle, text) -> bool, für Transkript-Farben
    operator_text_for: callable = None   # (handle) -> str | None: voller Operator-Text (Sprechbeginn)
    on_agent_presence: callable = None   # async (present: bool): Werksrolle aus/an


def _decode(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


TOPIC_TRANSCRIPT = "transcript"
# Sprechbeginn/-ende einer Operator-Ausgabe (UI-Paket 6): eigenes Topic, damit
# `transcript` weiter "gesprochen" heißt (CLI-Echo-Semantik unverändert).
TOPIC_TRANSCRIPT_LIVE = "transcript.live"


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


async def publish_notice(room: Room | None, payload: dict) -> bool:
    """operator.notice an alle im Raum (Operator-CLI + Browser), zuverlässig zugestellt.
    Fehler werden geloggt, nie geworfen."""
    if room is None:
        return False
    try:
        await room.local_participant.publish_data(
            payload=json.dumps(payload).encode(), topic=TOPIC_NOTICE, reliable=True
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[operator.notice publish] %s", e)
        return False


def speak_notice(session: AgentSession, text: str, *, live: bool = False) -> None:
    """Kurze Systemansage (z. B. low_balance) ohne auf das Ende zu warten; der Nutzer
    darf sie unterbrechen. Live: Realtime-Modell hat kein say(), wörtliche Anweisung."""
    logger.info("[operator.notice]%s say %s", " (live)" if live else "", text)
    try:
        if live:
            session.generate_reply(instructions=f"Sag jetzt wörtlich und nur das: {text}",
                                   allow_interruptions=True)
        else:
            session.say(text, allow_interruptions=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[operator.notice say] %s", e)


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
    # Zuletzt ausgegebene Operator-Ausgaben (Handle-ids + Texte) -> Transkript rot/blau
    operator_handles: set[int] = set()
    operator_texts: list[str] = []
    held: dict = {"text": None, "task": None}

    def _speak(text: str, seq: object = None) -> None:
        logger.info("[operator.say]%s %s", " (live)" if live else "", text[:200])
        if live:
            # Realtime-Modell spricht selbst: Operator-Text wird Anweisung (Inhalt
            # vollständig, bei Markierung/Transkript/Zitat wörtlich, live_say_user_input).
            # Das tatsächlich Gesprochene publiziert der Worker (conversation_item_added).
            # als markierter User-Turn (role=user); instructions= würde als
            # role="model"-Turn ankommen und Gemini hielte es für eigenes Gerede
            from .live import live_say_user_input

            handle = session.generate_reply(
                user_input=live_say_user_input(text), allow_interruptions=True
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
            operator_handles.add(id(handle))
        operator_texts.append(text)
        del operator_texts[:-20]

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

    # Werksrolle (voicehook-Guide) vs. Agent im Raum. Lock: Join/Leave/Persona dürfen
    # sich beim Umschalten nicht überholen (jedes Umschalten awaitet das Modell).
    role = {"agent": False, "persona": False}
    role_lock = asyncio.Lock()

    async def _set_role(normal_instructions: str, live_turn: str) -> None:
        if live:
            # Realtime: update_instructions wäre ein model-Turn -> markierter User-Turn
            ctx = agent.chat_ctx.copy()
            ctx.add_message(role="user", content=live_turn)
            await agent.update_chat_ctx(ctx)
        else:
            await agent.update_instructions(normal_instructions)

    async def on_agent_presence(present: bool) -> None:
        """Externer Agent kommt (Werksrolle aus) oder geht (Werksrolle wieder an).

        Eine Operator-Persona ersetzt weiterhin alles: kommt sie vor dem Umschalten,
        bleibt sie stehen. Geht der letzte Agent, gilt wieder die Werksrolle.
        """
        from .live import LIVE_AGENT_JOINED_USER, LIVE_AGENT_LEFT_USER

        async with role_lock:
            if present == role["agent"]:
                return
            role["agent"] = present
            if present:
                if role["persona"]:
                    logger.info("[role] agent joined, operator persona bleibt")
                    return
                await _set_role(OPERATOR_PERSONA, LIVE_AGENT_JOINED_USER)
                logger.info("[role] agent joined, Werksrolle aus%s", " (live)" if live else "")
            else:
                role["persona"] = False
                await _set_role(DEFAULT_PERSONA, LIVE_AGENT_LEFT_USER)
                logger.info("[role] agent left, Werksrolle an%s", " (live)" if live else "")

    async def on_persona(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        from .live import LIVE_PERSONA_USER

        async with role_lock:
            role["persona"] = True
            await _set_role(text, LIVE_PERSONA_USER.format(text=text))
        logger.info("[operator.persona]%s %d chars injected", " (live)" if live else "", len(text))

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

    def is_operator_speech(handle: object, text: str) -> bool:
        """Stammt eine gesprochene Zeile vom Operator (rot) oder vom Agent selbst (blau)?"""
        if handle is not None and id(handle) in operator_handles:
            return True
        t = (text or "").strip()
        if not t or live:  # live: Gemini formuliert Operator-Vorgaben um -> nur Handle zählt
            return False
        return any(o == t or o.startswith(t) for o in operator_texts)

    def operator_text_for(handle: object) -> str | None:
        """Voller Text der Operator-Ausgabe hinter `handle` (für transcript.live beim
        Sprechbeginn), sonst None. Live-Modus: None, Gemini formuliert um, der Text
        steht erst nach dem Sprechen fest."""
        if live or handle is None or id(handle) not in operator_handles:
            return None
        for _seq, text, h in pending:
            if h is handle:
                return text
        return None

    return RelayHandlers(
        is_operator_speech=is_operator_speech,
        operator_text_for=operator_text_for,
        on_say=on_say, on_persona=on_persona, on_mode=on_mode,
        on_interrupt=on_interrupt, on_inject=on_inject,
        on_agent_presence=on_agent_presence,
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
