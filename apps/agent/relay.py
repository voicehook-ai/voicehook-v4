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
- operator.say        — TTS the text über die Operator-Queue (nie verloren): gesprochen
                        wird nur, wenn der Nutzer nicht spricht (SAY_QUIET_S Stille);
                        unterbricht er, kommt der ungesprochene Rest vorne wieder in die
                        Queue; liegt etwas in der Queue, antwortet Delta nicht selbst.
                        Ist nichts Ungesprochenes offen: einreihen.
                        Sonst: Ausgabe stoppen, ungesprochene Aussagen per
                        operator.revise ans Brain zurück, neue Aussage halten, bis
                        das Brain mit mode "overwrite" die Zusammenfassung schickt
                        (nach HOLD_S spricht die gehaltene). mode "append": anhängen.
- operator.revise     — (Agent -> Operator) {unspoken:[...], new, text:Anweisung}
- operator.persona    — Wissen des Agenten: bereinigt und als eigener Block HINTER den
                        festen Delta-Kern gehängt (core.py); ersetzt den Kern nie.
- operator.mode       — switch strict/auto generation ({"mode":"strict"|"auto"})
- operator.interrupt  — alles stoppen, ungesprochene Aussagen per operator.revise melden
- operator.inject     — synthetic user-turn (test harness; operator reads transcript)
- operator.say_status (Agent -> Operator) {seq, state, spoken_chars} je Aussage:
                        queued|spoken|interrupted|requeued|replaced
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
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from livekit.agents import Agent, StopResponse
from livekit.agents import llm as lk_llm

from .activity import (
    TOPIC_ACTIVITY,
    activity_block,
    activity_interval_s,
    activity_sentence,
    normalize_activity,
)
from .alive import (
    TOPIC_ALIVE,
    OperatorAlive,
    live_reachable_user,
    live_unreachable_user,
    unreachable_block,
    unreachable_sentence,
)
from .board import board_block, normalize_board, status_sentence
from .clock import now_block, system_now
from .core import (
    FirstLine,
    agent_mark,
    clean_spoken,
    compose,
    core_normal,
    history_turns,
    is_agent_item,
    mark_agent_items,
    persona_block,
    sanitize_persona,
    wait_line,
)
from .guide import VOICEHOOK_GUIDE, agent_refs
from .history import HistoryKeeper
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
TOPIC_SAY_STATUS = "operator.say_status"  # agent -> operator: Verbleib jeder Aussage
TOPIC_NOTICE = "operator.notice"   # agent -> alle: Hinweis (low_balance), Browser + Operator
TOPIC_STATUS = "operator.status"   # operator -> agent: Board {doing, open[], done[]} (board.py)
TOPIC_STATUS_REQUEST = "operator.status_request"  # agent -> operator: Nutzer fragt nach dem Stand

STATUS_MIN_INTERVAL_S = 5.0  # höchstens 1 Board-Update je 5 s und Raum, das letzte gewinnt
STATUS_ANSWER_WINDOW_S = 8.0  # kommt das Board so schnell nach einer Nachfrage, sagt Delta es an
_STATUS_ID = "vh-status-"     # Live: fester Platz im Chat-Kontext (alte Status-Turns raus)
_ACTIVITY_ID = "vh-activity-"  # Live: fester Platz für den Aktivitäts-Feed

LOW_BALANCE_ANNOUNCEMENT = "Hey, Achtung, das Guthaben ist in wenigen Minuten leer."

HOLD_S = 8.0  # Olli-Regel "Stille ist der Killer, ab 8s ansagen": so lange wartet ein
             # zurückgehaltenes say auf das zusammengefasste overwrite des Brains

# Operator-Queue (Oliver 02.10.2026: "Claudes Sätze dürfen nie verloren gehen"). Prod
# lucid-lucid-flux-NPRT: 10 von 16 says nie hörbar, weil livekit jede say abbricht,
# sobald ein Nutzer-Turn endet (agent_activity.py _user_turn_completed_task), und ein
# abgebrochener Handle als erledigt galt.
SAY_QUIET_S_DEFAULT = 0.6   # so lange muss der Nutzer still sein, bevor eine say startet
SHORT_REST_CHARS = 15       # kürzerer Rest nach Abbruch: ganzen Satz neu (verständlich)


def say_quiet_s() -> float:
    """VOICEHOOK_SAY_QUIET_MS (Default 600): Stille vor einer Operator-Ausgabe."""
    import os

    raw = os.environ.get("VOICEHOOK_SAY_QUIET_MS", "")
    try:
        ms = float(raw)
    except ValueError:
        return SAY_QUIET_S_DEFAULT
    return ms / 1000.0 if ms >= 0 else SAY_QUIET_S_DEFAULT


def sentence_restart(full: str, rest: str) -> str:
    """Kurzer Rest nach Abbruch: ab dem Anfang des Satzes, in dem abgebrochen wurde."""
    full = (full or "").strip()
    cut = full.rfind(rest) if rest else -1
    if cut < 0:
        cut = max(0, len(full) - len(rest))
    head = full[:cut]
    ends = [i for i, ch in enumerate(head) if ch in ".!?" and (i + 1 == len(head) or head[i + 1].isspace())]
    if not ends:
        return full
    return full[ends[-1] + 1:].strip() or full

# Neutrale Sprachrohr-Rolle ohne Werksrolle: gilt, sobald ein externer Agent
# (vh.role=agent) im Raum ist und noch keine eigene Persona geschickt hat. Kern
# (core.py) steht immer vorn; Wartesätze, Name statt "Operator", keine
# Rechtfertigung stehen dort (Oliver 02.10.2026).
def agent_role(name: str | None = None) -> str:
    """Schicht 2 bei Agent im Raum ohne Persona: neutrales Sprachrohr, kein Verkäufer."""
    nom = agent_refs(name)["nom"]
    who = nom[:1].upper() + nom[1:]
    return (f"Rolle: {who} ist im Raum. Du bist jetzt die Stimme von {agent_refs(name)['dat']}, "
            "keine Werbung für voicehook. Simple Fragen zum Gespräch "
            f"beantwortest du kurz, alles Inhaltliche, Technische oder Unbekannte beantwortet {who}.")


def operator_persona(name: str | None = None) -> str:
    """Vollständige Instructions: Agent im Raum, keine Persona (Kern + Rolle + Anker)."""
    return compose(core_normal(name), agent_role(name))


OPERATOR_PERSONA = operator_persona()  # ohne Namen: "dein Agent"

# Werksrolle ohne Agent: Kern + voicehook-Guide + Anker.
DEFAULT_PERSONA = compose(core_normal(None), VOICEHOOK_GUIDE)

BOARD_STALE_S = 300.0  # Status älter als 5 min: Delta verkauft ihn nicht als aktuell


def board_stale_note(name: str | None, minutes: int) -> str:
    akk = agent_refs(name)["akk"]
    return (f" Dieser Stand ist von vor {minutes} Minuten und kann veraltet sein. Fragt "
            f"dein Gegenüber nach dem Stand, sag: Ich frag {akk} kurz.")


CLOCK_ID = "vh-clock"


def with_clock(items: list, now) -> list:  # noqa: ANN001
    """Zeitblock (clock.now_block) direkt hinter die Instruktionen/System-Einträge.

    Pro Aufruf frisch; ein alter Block (gleiche id) wird ersetzt, nie gestapelt."""
    items = [i for i in items if getattr(i, "id", None) != CLOCK_ID]
    k = 0
    while k < len(items) and getattr(items[k], "role", None) in ("system", "developer"):
        k += 1
    msg = lk_llm.ChatMessage(id=CLOCK_ID, role="system", content=[now_block(now)])
    return items[:k] + [msg] + items[k:]


_AUTO = object()  # RelayAgent(speakers=...) nicht angegeben -> Schalter entscheidet


class RelayAgent(Agent):
    """Mouthpiece with two modes:
      - auto (default): the LLM answers simple questions from its persona
        (the pushed knowledge-graph); operator.say overrides substantive content.
      - strict: StopResponse on every turn — the LLM never speaks on its own."""

    def __init__(  # noqa: ANN001, ANN003
        self, *, instructions: str = "", strict: bool = False, gate=None, speakers=_AUTO,
        history=None, now=None, **kwargs
    ) -> None:
        super().__init__(instructions=instructions, **kwargs)
        self.strict = strict
        self.gate = gate  # SpeechGate: nur Sprache geht zur (minutenweise bezahlten) STT
        # Hauptsprecher-Filter: nur die Stimme des Hauptsprechers erreicht das LLM.
        # Ohne Angabe entscheidet der Schalter VOICEHOOK_STT_DIARIZE (Default an).
        if speakers is _AUTO:
            speakers = PrimarySpeakerFilter() if diarize_enabled() else None
        self.speakers = speakers
        self.agent_name: str | None = None  # Name des Agenten im Raum (clean_spoken)
        # Per operator.say gesprochene Sätze des Agenten: llm_node markiert sie in der
        # Kontextkopie als "[Name] ...", damit Delta sie nicht für eigene hält.
        self.operator_said: list[str] = []
        # Verlauf (#9): letzte Wechsel + laufende Zusammenfassung (history.py). Ohne
        # Angabe nur kappen; der Worker gibt im Normalmodus den Gemini-Zusammenfasser mit.
        self.history = history if history is not None else HistoryKeeper(history_turns())
        # Zusammenfassung: Agentensätze behalten ihre Markierung ("[Claude] sagte: ...")
        self.history.who = lambda item: (agent_mark(self.agent_name)
                                         if is_agent_item(item, self.operator_said) else None)
        # Datum/Uhrzeit (clock.py): eigene Schicht, pro Antwort neu, Kern bleibt fest.
        self.now = now or system_now

    def llm_node(self, chat_ctx, tools, model_settings):  # noqa: ANN001, ANN201
        """Normal-Pipeline: Verlauf kappen/zusammenfassen (Instruktionen mit Kern,
        Persona und Status bleiben immer) und Deltas eigene Ausgabe bereinigen
        (Markdown, Emojis, "Operator" -> Name). Agentensätze werden nur in der Kopie
        markiert ("[Claude] ..." als user-Turn), der gespeicherte Verlauf bleibt. Live nutzt diesen
        Knoten nicht."""
        items = list(chat_ctx.items)
        ctx = chat_ctx.copy()
        window = mark_agent_items(self.history.context(items), self.operator_said, self.agent_name)
        ctx.items = with_clock(window, self.now())
        return self._clean_stream(Agent.default.llm_node(self, ctx, tools, model_settings), items)

    async def _clean_stream(self, stream, items):  # noqa: ANN001, ANN202
        """Bereinigen; ist die erste Zeile ein Wartesatz, nach ihr kappen (core.FirstLine):
        gegen gestapelte Wartesätze entscheidet der Code, nicht der Prompt. Echte Antworten
        aus Status/Wissen laufen vollständig durch (Zeilen verbunden)."""
        self.history.begin()
        first = FirstLine(agent_mark(self.agent_name), wait_line(self.agent_name))
        try:
            async for chunk in stream:
                if isinstance(chunk, str):
                    text = first.feed(chunk)
                    if text:
                        yield clean_spoken(text, self.agent_name)
                    if first.done:
                        break
                    continue
                delta = getattr(chunk, "delta", None) if isinstance(chunk, lk_llm.ChatChunk) else None
                if delta is not None and delta.content:
                    delta.content = clean_spoken(first.feed(delta.content), self.agent_name)
                yield chunk
                if first.done:
                    break
            rest = first.flush()
            if rest:
                yield clean_spoken(rest, self.agent_name)
        finally:
            self.history.end(items)  # nach der Antwort: Zusammenfassung im Hintergrund

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
        # Vorrang vor Delta: liegt eine Operator-Aussage in der Queue (z. B. der Rest, den
        # dieser Nutzer-Turn gerade abgebrochen hat), keine eigene Antwort, die Queue
        # spielt. Die Nutzeräußerung bleibt im Verlauf (StopResponse verwirft sie sonst).
        pending = getattr(self, "operator_say_pending", None)
        if callable(pending) and pending():
            msg = kwargs.get("new_message", args[1] if len(args) > 1 else None)
            if msg is not None:
                with contextlib.suppress(Exception):
                    self._chat_ctx.items.append(msg)
            logger.info("[operator.say] Queue hat Vorrang, keine eigene Antwort")
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
    on_agent_presence: callable = None   # async (present, name=None): Werksrolle aus/an
    on_status: callable = None           # operator.status: Board ersetzen (rate-limited)
    on_user_text: callable = None        # async (text): Nachfrage nach dem Stand -> status_request
    on_alive: callable = None            # operator.alive: Lebenszeichen des Agenten
    check_reach: callable = None         # async (): erreichbar/unerreichbar neu bewerten (Takt)
    on_user_state: callable = None       # (ev|state): Operator-Queue wartet, solange er spricht
    on_activity: callable = None         # operator.activity: Feed ersetzen (rate-limited)


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
    status_interval_s: float = STATUS_MIN_INTERVAL_S,
    activity_interval: float | None = None,  # None: activity_interval_s(live) (Env/Default)
    clock=None,  # noqa: ANN001  Tests: monotone Uhr injizieren
    say_quiet: float | None = None,  # Tests: Stille vor einer say (sonst Env/Default)
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
    held: dict = {"text": None, "task": None, "seq": None}
    # revise-Runde offen (operator.revise gesendet, overwrite steht aus): ein overwrite
    # ersetzt dann nur, was beim revise offen war (schon abgebrochen + gehaltene Aussage),
    # says, die danach kamen, bleiben in der Queue (E2E 02.10.: say 9 verschluckt).
    rev = {"open": False}
    spoke: dict = {"handle": None}  # Handle der letzten _speak-Ausgabe

    def _speak(text: str, seq: object = None, *, live_input: str | None = None) -> None:
        logger.info("[operator.say]%s %s", " (live)" if live else "", text[:200])
        if live:
            # Realtime-Modell spricht selbst: Operator-Text wird Anweisung (Inhalt
            # vollständig, bei Markierung/Transkript/Zitat wörtlich, live_say_user_input).
            # Das tatsächlich Gesprochene publiziert der Worker (conversation_item_added).
            # als markierter User-Turn (role=user); instructions= würde als
            # role="model"-Turn ankommen und Gemini hielte es für eigenes Gerede
            from .live import live_say_user_input

            handle = session.generate_reply(
                user_input=live_input or live_say_user_input(text), allow_interruptions=True
            )
        else:
            # Transkript kommt vom Worker (conversation_item_added) mit dem tatsächlich
            # Gesprochenen, auch für Eigenantworten; hier nicht doppelt senden.
            # allow_interruptions=True = full-duplex barge-in: the user can comment
            # while the mouthpiece is speaking and the STT keeps hearing them.
            handle = session.say(text, allow_interruptions=True)
        pending[:] = [p for p in pending if not _is_done(p[2])]
        if handle is not None:
            spoke["handle"] = handle  # Operator-Queue: done-Callback (Nachsprechen)
            pending.append((seq, text, handle))
            operator_handles.add(id(handle))
        operator_texts.append(text)
        del operator_texts[:-20]
        said = getattr(agent, "operator_said", None)
        if isinstance(said, list):  # llm_node markiert diese Sätze als "[Name] ..."
            said.append(text)
            del said[:-50]

    # ----- Operator-Queue: nichts geht verloren ---------------------------------------
    # Immer nur EINE Operator-Ausgabe bei livekit: Bricht ein Nutzer-Turn sie ab, muss
    # der Rest VOR später eingereihten Aussagen kommen (livekits eigene Queue spielte die
    # nächste sonst sofort). Gesprochen wird erst, wenn der Nutzer quiet_s still ist.
    quiet_s = say_quiet_s() if say_quiet is None else say_quiet
    queue: deque[dict] = deque()     # {seq, text, full, spoken}
    cur: dict = {"item": None, "handle": None}
    cancelled: set[int] = set()      # id(handle), die wir selbst abgebrochen haben
    user = {"speaking": False, "quiet_since": None, "timer": None}
    seq_n = [0]

    def _seq(seq: object) -> object:
        if seq is not None:
            return seq
        seq_n[0] += 1
        return f"vh-{seq_n[0]}"  # say ohne seq (alte Clients): eigene Kennung

    def _say_status(seq: object, state: str, spoken_chars: int = 0) -> None:
        logger.info("[operator.say_status] seq=%s state=%s spoken_chars=%d", seq, state, spoken_chars)
        if room is None:
            return
        payload = json.dumps({"seq": seq, "state": state, "spoken_chars": spoken_chars}).encode()

        async def _send() -> None:
            try:
                await room.local_participant.publish_data(payload=payload, topic=TOPIC_SAY_STATUS,
                                                          reliable=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("[operator.say_status publish] %s", e)
        with contextlib.suppress(RuntimeError):  # kein laufender Loop (sync Tests)
            asyncio.get_running_loop().create_task(_send())

    def _cur_open() -> bool:
        h = cur["handle"]
        return h is not None and not _is_done(h)

    def _user_wait() -> float | None:
        """None: Nutzer spricht (auf Event warten); sonst Sekunden bis Sprechen erlaubt."""
        state = getattr(session, "user_state", None)
        if user["speaking"] or state == "speaking":
            return None
        qs = user["quiet_since"]
        return 0.0 if qs is None else max(0.0, qs + quiet_s - _now())

    def _arm(delay: float) -> None:
        if user["timer"] is not None:
            return
        def _fire() -> None:
            user["timer"] = None
            _pump()
        user["timer"] = asyncio.get_running_loop().call_later(delay, _fire)

    def _disarm() -> None:
        if user["timer"] is not None:
            user["timer"].cancel()
            user["timer"] = None

    def _pump() -> None:
        """Nächste Aussage an livekit, wenn nichts Eigenes läuft und der Nutzer still ist."""
        if not queue or _cur_open():
            return
        wait = _user_wait()
        if wait is None:
            return  # on_user_state ruft _pump, sobald er aufhört
        if wait > 0:
            _arm(wait)
            return
        item = queue.popleft()
        live_input = None
        if live and item["spoken"]:
            from .live import live_say_rest_user_input

            live_input = live_say_rest_user_input(item["full"], item["spoken"])
        spoke["handle"] = None
        try:
            _speak(item["text"], item["seq"], live_input=live_input)
        except Exception as e:  # noqa: BLE001 — Session weg: Aussage bleibt in der Queue
            logger.warning("[operator.say] say fehlgeschlagen, bleibt in der Queue: %s", e)
            queue.appendleft(item)
            return
        handle = spoke["handle"]
        cur["item"], cur["handle"] = item, handle
        if handle is None:
            return
        add = getattr(handle, "add_done_callback", None)
        if callable(add):
            with contextlib.suppress(Exception):
                add(_on_done)

    def _on_done(handle: object) -> None:
        """say fertig: gesprochen, oder unterbrochen -> Rest vorne wieder in die Queue."""
        if cur["handle"] is not handle:
            cancelled.discard(id(handle))
            return
        item = cur["item"]
        cur["item"], cur["handle"] = None, None
        if id(handle) in cancelled:  # overwrite/revise/interrupt: Status kam dort
            cancelled.discard(id(handle))
            _pump()
            return
        spoken = _spoken_text(handle)
        if getattr(handle, "interrupted", False) is True:
            _say_status(item["seq"], "interrupted", len(spoken))
            if live:
                # Live: kein Nachsprechen (Oliver 02.10.2026, Raum vivid-orbit-fresh-V32N).
                # Gemini formuliert um und das Ausgabe-Transkript hinkt dem Audio nach:
                # spoken_chars=8, gehört hatte der Nutzer deutlich mehr, der "Rest" kam
                # als ganze Aussage nochmal. Wer unterbricht, will selbst reden.
                # `interrupted` ist der Endzustand, der Agent entscheidet über Neues.
                _pump()
                return
            else:
                rest = unspoken_rest(item["text"], spoken)
                if rest and len(rest) < SHORT_REST_CHARS:
                    rest = sentence_restart(item["text"], rest)
                nxt = {**item, "text": rest}
            if rest:
                queue.appendleft(nxt)
                _say_status(item["seq"], "requeued", len(spoken))
            else:  # alles war draußen (Abbruch genau am Ende): gilt als gesprochen
                _say_status(item["seq"], "spoken", len(spoken))
        else:
            _say_status(item["seq"], "spoken", len(spoken) or len(item["text"]))
        _pump()

    def _enqueue(text: str, seq: object, *, front: bool = False) -> None:
        item = {"seq": seq, "text": text, "full": text, "spoken": ""}
        if front:
            queue.appendleft(item)
        else:
            queue.append(item)
        _say_status(seq, "queued")
        _pump()

    def has_pending() -> bool:
        """Operator-Aussage offen (Queue, laufend oder gehalten)? -> Delta schweigt."""
        busy = bool(queue) or _cur_open() or held["text"] is not None
        if busy:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().call_soon(_pump)
        return busy

    agent.operator_say_pending = has_pending

    def on_user_state(ev: object) -> None:
        new = getattr(ev, "new_state", ev)
        if new == "speaking":
            user["speaking"], user["quiet_since"] = True, None
            _disarm()
            return
        if user["speaking"]:
            user["speaking"], user["quiet_since"] = False, _now()
        _pump()

    _on = getattr(session, "on", None)
    if callable(_on):
        with contextlib.suppress(Exception):
            _on("user_state_changed", on_user_state)

    def _drop_hold() -> None:
        rev["open"] = False
        task = held["task"]
        if task is not None and not task.done():
            task.cancel()
        if held["text"] is not None:
            _say_status(held["seq"], "replaced")
        held["text"], held["task"], held["seq"] = None, None, None

    def _stop_session() -> None:
        # force=True: livekit wirft sonst RuntimeError statt zu stoppen, sobald die
        # laufende Ausgabe keine Unterbrechung erlaubt. Stoppt auch Eigenantworten.
        try:
            session.interrupt(force=True)
        except Exception as e:  # noqa: BLE001 — nichts läuft / Session gestoppt
            logger.debug("[operator.say] session.interrupt: %s", e)

    def _has_open() -> bool:
        return bool(queue) or any(not _is_done(p[2]) for p in pending)

    async def _cancel_open(state: str = "replaced") -> list[str]:
        """Stoppt alle offenen Ausgaben und leert die Queue, liefert die ungesprochenen
        Reste (für operator.revise). Jede betroffene seq bekommt `state`."""
        open_ = [p for p in pending if not _is_done(p[2])]
        pending.clear()
        queued = list(queue)
        queue.clear()
        _disarm()
        cur["item"], cur["handle"] = None, None
        for _s, _text, handle in open_:
            cancelled.add(id(handle))
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
        rest = []
        for seq_, text, h in open_:
            said = _spoken_text(h)
            _say_status(seq_, state, len(said))
            rest.append(unspoken_rest(text, said))
        for item in queued:
            _say_status(item["seq"], state)
            rest.append(item["text"])
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

    async def _speak_after_hold(text: str, seq: object) -> None:
        await asyncio.sleep(hold_s)
        if held["text"] == text:
            held["text"], held["task"], held["seq"] = None, None, None
            rev["open"] = False
            logger.info("[operator.say] kein overwrite in %.1fs, spreche gehaltene Aussage", hold_s)
            _enqueue(text, seq, front=True)  # vor says, die während des Haltens kamen

    async def on_say(packet: DataPacket) -> None:
        data = _decode(packet.data)
        text = (data.get("text") or "").strip()
        if not text:
            return
        seq = _seq(data.get("seq", data.get("_seq")))  # CLI taggt _seq
        mode = (data.get("mode") or "revise").strip().lower()
        if mode == "append":
            _enqueue(text, seq)
            return
        if mode == "overwrite":
            if rev["open"]:
                # Antwort auf operator.revise: ersetzt nur die gehaltene Aussage (die alten
                # Reste sind schon abgebrochen). Neuere says bleiben, die Zusammenfassung
                # kommt vor sie, weil sie den älteren Stand ersetzt.
                _drop_hold()
                _enqueue(text, seq, front=True)
                return
            # Zusammenfassung ohne revise-Runde: ersetzt alles Offene, Gehaltene und Eingereihte
            _drop_hold()
            await _cancel_open("replaced")
            _enqueue(text, seq)
            return
        # Default revise
        interrupt = data.get("priority") == "interrupt"
        if not interrupt and (rev["open"] or not _cur_open()):
            # Nichts von uns spricht gerade (nur Wartendes: queued/requeued) oder eine
            # revise-Runde läuft schon: anhängen, keine neue revise-Runde. Revise nur, wenn
            # wirklich mitten im Sprechen ersetzt würde (E2E 02.10.: 4 von 11 says).
            _enqueue(text, seq)
            return
        _drop_hold()
        if interrupt:
            _stop_session()          # nur ausdrücklich: laufende Ausgabe abbrechen
        if not _has_open():
            # Nichts Eigenes offen: einreihen. Eine laufende Eigenantwort des Agents
            # (auto mode) spricht zu Ende, der Operator fällt ihm nicht ins Wort
            # (Olli 30.09.: "Operator say fällt ihm ins Wort").
            _enqueue(text, seq)
            return
        unspoken = await _cancel_open("replaced")
        if not unspoken:
            _enqueue(text, seq)
            return
        held["text"], held["seq"] = text, seq
        held["task"] = asyncio.create_task(_speak_after_hold(text, seq))
        rev["open"] = True
        _say_status(seq, "queued")
        await _ask_revise(unspoken, text)

    # Werksrolle (voicehook-Guide) vs. Agent im Raum. Lock: Join/Leave/Persona dürfen
    # sich beim Umschalten nicht überholen (jedes Umschalten awaitet das Modell).
    role = {"agent": False, "persona": None, "name": None, "user": None, "board": None,
            "unreachable": False,
            "board_at": None, "board_stale": False, "activity": None}
    role_lock = asyncio.Lock()
    _now = clock or (lambda: asyncio.get_running_loop().time())
    alive = OperatorAlive()  # operator.alive (alive.py): hört der Agent noch zu?

    def _board_minutes() -> int | None:
        at = role["board_at"]
        if role["board"] is None or at is None:
            return None
        age = _now() - at
        return int(age // 60) if age > BOARD_STALE_S else None

    def _normal_instructions() -> str:
        """KERN + Rolle (Persona-Wissen / Sprachrohr / Werks-Guide) + Status + Anker.

        Der Kern steht immer vorn und ist über den Datenkanal nicht änderbar; die
        Persona ist nur Wissen (core.persona_block). Board: fester Platz, ersetzt."""
        name = role["name"]
        if role["persona"]:
            layer = persona_block(role["persona"], name)
        elif role["agent"]:
            layer = agent_role(name)
        else:
            return DEFAULT_PERSONA
        status = board_block(role["board"], agent_refs(name)["nom"])
        mins = _board_minutes()
        if status and mins is not None:
            status += board_stale_note(name, mins)
        status += activity_block(role["activity"], agent_refs(name)["nom"])
        if role["unreachable"]:
            status += unreachable_block(name)
        return compose(core_normal(name, role["user"]), layer, status)

    def _clock_now():  # noqa: ANN202
        fn = getattr(agent, "now", None)
        t = fn() if callable(fn) else None
        return t if isinstance(t, datetime) else system_now()

    async def _set_role(normal_instructions: str, live_turn: str) -> None:
        if live:
            # Realtime: update_instructions wäre ein model-Turn -> markierter User-Turn
            ctx = agent.chat_ctx.copy()
            ctx.add_message(role="user", content=live_turn)
            await agent.update_chat_ctx(ctx)
        else:
            await agent.update_instructions(normal_instructions)

    async def on_agent_presence(present: bool, name: str | None = None,
                                user: str | None = None) -> None:
        """Externer Agent kommt (Werksrolle aus) oder geht (Werksrolle wieder an).

        `name`: Anzeigename des zuletzt beigetretenen Agenten (guide.agent_display_name,
        None = "dein Agent"). Wechselt er bei anwesendem Agent (zweiter Agent kommt,
        vh.name ändert sich), werden die Wartesätze live mit dem neuen Namen gesetzt.
        `user`: Name des Nutzers (vh.user, CLI --username), steht dann im Kern.
        Eine Operator-Persona bleibt stehen, der Kern wird aber mit Namen neu gesetzt
        (Bug 02.10.: Persona kam 1,3 s vor der Presence, Name blieb None). Geht der
        letzte Agent, gilt wieder die Werksrolle.
        """
        from .live import LIVE_AGENT_LEFT_USER, live_agent_joined_user

        name = name if present else None
        user = user if present else None
        async with role_lock:
            if present == role["agent"] and name == role["name"] and user == role["user"]:
                return
            joined = present and not role["agent"]
            role["agent"] = present
            role["name"] = name
            role["user"] = user
            agent.agent_name = name
            if joined or not present:
                alive.reset()  # neuer Agent: eigenes Lebenszeichen abwarten (legacy bis dahin)
                role["unreachable"] = False
            if present:
                await _set_role(_normal_instructions(), live_agent_joined_user(name, user))
                if role["persona"]:
                    logger.info("[role] agent present (%s), operator persona bleibt, Kern mit "
                                "Namen neu gesetzt", name or "-")
                    return
                logger.info("[role] agent %s (%s), Werksrolle aus%s",
                            "joined" if joined else "renamed", name or "-", " (live)" if live else "")
            else:
                role["persona"] = None
                role["board"] = None
                role["board_at"] = None
                role["activity"] = None
                await _set_role(DEFAULT_PERSONA, LIVE_AGENT_LEFT_USER)
                logger.info("[role] agent left, Werksrolle an%s", " (live)" if live else "")

    async def on_persona(packet: DataPacket) -> None:
        """Persona = Wissen des Agenten, nie Regeln: bereinigt, gekappt, Override-Sätze
        gestrichen (Code entscheidet) und HINTER den festen Kern gehängt."""
        data = _decode(packet.data)
        raw = (data.get("text") or "").strip()
        if not raw:
            return
        from .live import live_persona_user

        clean = sanitize_persona(raw)
        if clean.removed or clean.truncated:
            logger.warning("[operator.persona] %d Override-Saetze gestrichen%s: %s",
                           len(clean.removed), ", gekappt" if clean.truncated else "",
                           " | ".join(r[:80] for r in clean.removed)[:400])
            await publish_notice(room, {
                "kind": "persona_sanitized", "removed": len(clean.removed),
                "truncated": clean.truncated,
                "text": "Persona ist nur Wissen: Regel-Overrides wurden gestrichen, der "
                        "Delta-Kern gilt immer.",
            })
        if not clean.text:
            return
        async with role_lock:
            role["persona"] = clean.text
            await _set_role(_normal_instructions(), live_persona_user(clean.text, role["name"]))
        logger.info("[operator.persona]%s %d chars injected (raw %d)", " (live)" if live else "",
                    len(clean.text), len(raw))

    # ----- Status-Board (operator.status) + Nachfrage (operator.status_request) -----
    st: dict = {"last": None, "pending": None, "task": None, "request_at": None, "n": 0}

    async def _apply_status_live() -> None:
        from .live import live_stamp, live_status_user

        st["n"] += 1
        ctx = agent.chat_ctx.copy()
        items = getattr(ctx, "items", None)
        if isinstance(items, list):  # alter Stand raus: lokaler Kontext bleibt konstant
            items[:] = [i for i in items if not str(getattr(i, "id", "")).startswith(_STATUS_ID)]
        ctx.add_message(role="user", content=live_stamp(live_status_user(role["board"], role["name"]),
                                                     _clock_now()),
                        id=f"{_STATUS_ID}{st['n']}")
        await agent.update_chat_ctx(ctx)

    async def _apply_status() -> None:
        board, st["pending"] = st["pending"], None
        st["last"] = _now()
        async with role_lock:
            role["board"] = board
            role["board_at"] = st["last"]
            role["board_stale"] = False
            if live:
                await _apply_status_live()
            else:
                await agent.update_instructions(_normal_instructions())
        logger.info("[operator.status]%s %s", " (live)" if live else "",
                    "cleared" if board is None else f"{len(board['open'])} open, {len(board['done'])} done")
        req = st["request_at"]
        if req is not None and _now() - req <= STATUS_ANSWER_WINDOW_S:
            st["request_at"] = None
            refs = agent_refs(role["name"])
            # doing ist nicht nötig: ohne doing antwortet der Aktivitäts-Feed (Oliver 02.10.)
            line = status_sentence(board, refs["nom"]) or activity_sentence(role["activity"], refs["dat"])
            if line:
                speak_notice(session, line, live=live)

    async def _apply_later(delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await _apply_status()
        finally:
            st["task"] = None

    async def on_status(packet: DataPacket) -> None:
        """Board ersetzen. Rate-Limit: höchstens 1 Update je status_interval_s, das
        letzte im Fenster gewinnt (wird nach Ablauf angewandt)."""
        st["pending"] = normalize_board(_decode(packet.data))
        wait = 0.0 if st["last"] is None else st["last"] + status_interval_s - _now()
        if wait <= 0 and st["task"] is None:
            await _apply_status()
        elif st["task"] is None:
            st["task"] = asyncio.create_task(_apply_later(wait))

    # ----- Aktivitäts-Feed (operator.activity, activity.py) --------------------------
    act: dict = {"last": None, "pending": None, "task": None, "n": 0}
    act_interval = activity_interval_s(live) if activity_interval is None else activity_interval

    async def _apply_activity() -> None:
        lines, act["pending"] = act["pending"], None
        act["last"] = _now()
        async with role_lock:
            if lines == role["activity"]:
                return
            role["activity"] = lines
            if live:
                from .live import live_activity_user, live_stamp

                act["n"] += 1
                ctx = agent.chat_ctx.copy()
                items = getattr(ctx, "items", None)
                if isinstance(items, list):  # alter Feed raus: lokaler Kontext bleibt konstant
                    items[:] = [i for i in items if not str(getattr(i, "id", "")).startswith(_ACTIVITY_ID)]
                ctx.add_message(role="user", id=f"{_ACTIVITY_ID}{act['n']}",
                                content=live_stamp(live_activity_user(lines, role["name"]), _clock_now()))
                await agent.update_chat_ctx(ctx)
            else:
                await agent.update_instructions(_normal_instructions())
        logger.info("[operator.activity]%s %s", " (live)" if live else "",
                    "cleared" if lines is None else f"{len(lines)} Zeilen")

    async def _apply_activity_later(delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await _apply_activity()
        finally:
            act["task"] = None

    async def on_activity(packet: DataPacket) -> None:
        """Feed ersetzen (nie anhängen). Rate-Limit: höchstens 1 Update je act_interval
        (Normal 5 s, Live 20 s: dort kostet jedes Update einen Turn mit ganzem Kontext),
        das letzte im Fenster gewinnt. Kommt er vor der Presence, gilt er ab dem Join."""
        act["pending"] = normalize_activity(_decode(packet.data))
        wait = 0.0 if act["last"] is None else act["last"] + act_interval - _now()
        if wait <= 0 and act["task"] is None:
            await _apply_activity()
        elif act["task"] is None:
            act["task"] = asyncio.create_task(_apply_activity_later(wait))

    async def on_user_text(text: str) -> None:
        """Fragt der Nutzer nach dem Stand des Agenten: operator.status_request an ihn
        (Code entscheidet per Muster). Höchstens eine offene Nachfrage je Fenster."""
        from .board import is_status_question

        if not role["agent"] or room is None or not is_status_question(text):
            return
        if role["unreachable"]:
            # Code entscheidet: kein status_request ins Leere, fester Satz statt Warten
            speak_notice(session, unreachable_sentence(role["name"]), live=live)
            return
        req = st["request_at"]
        if req is not None and _now() - req <= STATUS_ANSWER_WINDOW_S:
            return
        st["request_at"] = _now()
        try:
            await room.local_participant.publish_data(
                payload=json.dumps({"text": text[:200]}).encode(),
                topic=TOPIC_STATUS_REQUEST, reliable=True)
            logger.info("[operator.status_request] sent")
        except Exception as e:  # noqa: BLE001
            logger.warning("[operator.status_request publish] %s", e)

    # ----- Lebenszeichen (operator.alive) -------------------------------------------
    async def check_reach() -> None:
        """Agent im Raum, aber ohne Lebenszeichen (> ALIVE_STALE_S) oder alive:false:
        Wartesätze werden zum festen Satz. Wird vom Worker im Takt und bei jedem
        Lebenszeichen aufgerufen; schaltet nur bei Zustandswechsel um."""
        async with role_lock:
            stale = _board_minutes() is not None
            if stale != role["board_stale"]:
                # Status wird alt: einmal neu setzen (Normal; Live behält den Turn)
                role["board_stale"] = stale
                if stale and not live:
                    await agent.update_instructions(_normal_instructions())
            unreach = role["agent"] and not alive.reachable(role["agent"], _now())
            if unreach == role["unreachable"]:
                return
            role["unreachable"] = unreach
            live_turn = (live_unreachable_user(role["name"]) if unreach
                         else live_reachable_user(role["name"]))
            await _set_role(_normal_instructions(), live_turn)
        logger.info("[operator.alive] agent %s%s", "unerreichbar" if unreach else "wieder erreichbar",
                    " (live)" if live else "")

    async def on_alive(packet: DataPacket) -> None:
        alive.on_packet(_decode(packet.data), _now())
        await check_reach()

    async def on_mode(packet: DataPacket) -> None:
        data = _decode(packet.data)
        mode = (data.get("mode") or "").strip().lower()
        agent.strict = mode == "strict"
        logger.info("[operator.mode] strict=%s", agent.strict)

    async def on_interrupt(_packet: DataPacket) -> None:
        _drop_hold()
        unspoken = await _cancel_open("interrupted")
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
        on_status=on_status, on_user_text=on_user_text,
        on_alive=on_alive, check_reach=check_reach,
        on_user_state=on_user_state, on_activity=on_activity,
    )


def topic_dispatch(handlers: RelayHandlers) -> dict[str, callable]:
    """Map operator.* topic → handler. Used by the worker's data-channel subscription."""
    return {
        TOPIC_SAY: handlers.on_say,
        TOPIC_PERSONA: handlers.on_persona,
        TOPIC_MODE: handlers.on_mode,
        TOPIC_INTERRUPT: handlers.on_interrupt,
        TOPIC_INJECT: handlers.on_inject,
        TOPIC_STATUS: handlers.on_status,
        TOPIC_ALIVE: handlers.on_alive,
        TOPIC_ACTIVITY: handlers.on_activity,
    }
