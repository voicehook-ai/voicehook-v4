"""Operator-Queue: Claudes Sätze gehen nie verloren (Prod 02.10.2026, lucid-lucid-flux-NPRT:
von 16 operator.say nur 3 vollständig gesprochen). livekit 1.8.3 bricht jede laufende say
ab, sobald ein Nutzer-Turn endet (agent_activity.py _user_turn_completed_task) oder der
Nutzer losspricht (_interrupt_by_audio_activity). Die Fakes bilden genau das nach:
interrupted=True, chat_items = der bis dahin gesprochene Teil, dann done-Callbacks."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import StopResponse

from agent.relay import (
    TOPIC_SAY_STATUS,
    RelayAgent,
    build_relay_handlers,
    sentence_restart,
)

QUIET = 0.05


class _Msg:
    def __init__(self, text: str) -> None:
        self.text_content = text


class FakeHandle:
    """SpeechHandle-Attrappe mit dem Verhalten von livekit 1.8.3 (speech_handle.py)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.interrupted = False
        self.chat_items: list = []
        self._done = False
        self._cbs: list = []
        self.cut_at: str | None = None  # was bei einem Abbruch schon gesprochen ist

    def done(self) -> bool:
        return self._done

    def add_done_callback(self, cb) -> None:  # noqa: ANN001
        if self._done:
            asyncio.get_running_loop().call_soon(cb, self)
        else:
            self._cbs.append(cb)

    def finish(self, spoken: str | None = None, *, interrupted: bool = False) -> None:
        if self._done:
            return
        self.interrupted = interrupted
        said = self.text if spoken is None else spoken
        self.chat_items = [_Msg(said)] if said else []
        self._done = True
        for cb in self._cbs:
            cb(self)

    def interrupt(self, force: bool = False) -> FakeHandle:  # noqa: ARG002
        self.finish(self.cut_at or "", interrupted=True)
        return self

    async def wait_for_playout(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []
        self.user_state = "listening"
        self.listeners: dict = {}
        self.interrupt = MagicMock()

    @property
    def said(self) -> list[str]:
        return [h.text for h in self.handles]

    def on(self, ev: str, cb) -> None:  # noqa: ANN001
        self.listeners[ev] = cb

    def say(self, text: str, allow_interruptions: bool = True) -> FakeHandle:
        assert allow_interruptions is True
        h = FakeHandle(text)
        self.handles.append(h)
        return h

    def generate_reply(self, user_input: str = "", allow_interruptions: bool = True) -> FakeHandle:
        return self.say(user_input, allow_interruptions)

    def user(self, state: str) -> None:
        """Wie livekit: user_state_changed über session.on."""
        self.user_state = state
        ev = MagicMock(new_state=state)
        self.listeners["user_state_changed"](ev)


def _room() -> MagicMock:
    room = MagicMock()
    room.local_participant.publish_data = AsyncMock()
    return room


def _pkt(payload: dict):  # noqa: ANN202
    p = MagicMock()
    p.data = json.dumps(payload).encode()
    return p


def _status(room: MagicMock) -> list[tuple]:
    out = []
    for c in room.local_participant.publish_data.await_args_list:
        if c.kwargs.get("topic") == TOPIC_SAY_STATUS:
            d = json.loads(c.kwargs["payload"].decode())
            out.append((d["seq"], d["state"]))
    return out


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def _build(live: bool = False):  # noqa: ANN202
    session, agent, room = FakeSession(), RelayAgent(instructions="x"), _room()
    h = build_relay_handlers(session, agent, room=room, live=live, say_quiet=QUIET, hold_s=30)
    return session, agent, room, h


LONG = "Der Fix ist gebaut und getestet. Der Pull Request wartet auf deine Freigabe."


# ── 1. Nutzer-Turn mitten in der say: der Rest wird nachgesprochen ─────────────────
@pytest.mark.asyncio
async def test_user_turn_mid_say_rest_is_spoken_again():
    session, agent, room, h = _build()
    await h.on_say(_pkt({"text": LONG, "_seq": 7}))
    assert session.said == [LONG]
    # Nutzer spricht los, livekit bricht ab (user_turn): erster Satz war draußen
    session.user("speaking")
    session.handles[0].finish("Der Fix ist gebaut und getestet.", interrupted=True)
    assert session.said == [LONG]                        # nicht in den Nutzer hinein
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    assert session.said[-1] == "Der Pull Request wartet auf deine Freigabe."
    session.handles[-1].finish()
    await _drain()
    assert _status(room) == [(7, "queued"), (7, "interrupted"), (7, "requeued"), (7, "spoken")]


@pytest.mark.asyncio
async def test_interrupted_after_last_word_counts_as_spoken():
    # E2E fragments_call 02.10.: der letzte Rest lief zu Ende, livekit meldete trotzdem
    # interrupted (Nutzer setzte genau am Ende ein). Ohne Rest kein requeued, also muss
    # spoken kommen, sonst bleibt die say für den Operator ewig offen.
    session, _agent, room, h = _build()
    await h.on_say(_pkt({"text": LONG, "_seq": 3}))
    session.handles[0].finish(LONG, interrupted=True)
    await _drain()
    assert _status(room) == [(3, "queued"), (3, "interrupted"), (3, "spoken")]
    assert session.said == [LONG]


@pytest.mark.asyncio
async def test_short_rest_restarts_the_whole_sentence():
    session, _agent, _room_, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("Der Fix ist gebaut und getestet. Der Pull Request wartet auf deine",
                              interrupted=True)
    await asyncio.sleep(QUIET * 3)
    # Rest "Freigabe." wäre unverständlich: ganzer zweiter Satz neu
    assert session.said[-1] == "Der Pull Request wartet auf deine Freigabe."


@pytest.mark.asyncio
async def test_interrupted_before_any_word_repeats_everything():
    session, _agent, _room_, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("", interrupted=True)
    await asyncio.sleep(QUIET * 3)
    assert session.said == [LONG, LONG]


def test_sentence_restart():
    assert sentence_restart("Eins ist da. Zwei kommt jetzt.", "jetzt.") == "Zwei kommt jetzt."
    assert sentence_restart("Ohne Satzende hier", "hier") == "Ohne Satzende hier"


# ── 2. Queue bei sprechendem Nutzer: erst danach ────────────────────────────────────
@pytest.mark.asyncio
async def test_queue_waits_while_user_speaks_then_quiet_window():
    session, _agent, room, h = _build()
    session.user("speaking")
    await h.on_say(_pkt({"text": "Kurze Info.", "seq": 3}))
    await asyncio.sleep(QUIET * 2)
    assert session.said == []                            # Nutzer spricht: warten
    session.user("listening")
    await asyncio.sleep(QUIET / 5)
    assert session.said == []                            # Stille-Fenster noch nicht um
    await asyncio.sleep(QUIET * 2)
    assert session.said == ["Kurze Info."]
    await _drain()
    assert _status(room)[0] == (3, "queued")


@pytest.mark.asyncio
async def test_user_speaking_again_inside_quiet_window_postpones():
    session, _agent, _room_, h = _build()
    session.user("speaking")
    await h.on_say(_pkt({"text": "Info.", "seq": 1}))
    session.user("listening")
    await asyncio.sleep(QUIET / 3)
    session.user("speaking")                             # nächstes Fragment
    await asyncio.sleep(QUIET * 2)
    assert session.said == []
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    assert session.said == ["Info."]


# ── 3. Vorrang: keine eigene Delta-Antwort, solange die Queue voll ist ──────────────
@pytest.mark.asyncio
async def test_queue_has_priority_over_delta_reply():
    session, agent, _room_, h = _build()
    session.user("speaking")
    await h.on_say(_pkt({"text": "Antwort von Claude.", "seq": 1}))
    session.user("listening")
    msg = MagicMock()
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(MagicMock(), new_message=msg)
    assert agent._chat_ctx.items[-1] is msg               # Nutzeräußerung bleibt im Verlauf
    await asyncio.sleep(QUIET * 3)
    assert session.said == ["Antwort von Claude."]       # stattdessen spielt die Queue


@pytest.mark.asyncio
async def test_interrupted_say_blocks_delta_and_is_replayed():
    """Genau der Prod-Ablauf: Turn-Ende bricht die say ab, dann on_user_turn_completed."""
    session, agent, _room_, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("Der Fix ist gebaut und getestet.", interrupted=True)
    with pytest.raises(StopResponse):
        await agent.on_user_turn_completed(MagicMock(), new_message=MagicMock())
    await asyncio.sleep(QUIET * 3)
    assert session.said[-1] == "Der Pull Request wartet auf deine Freigabe."


@pytest.mark.asyncio
async def test_delta_answers_when_queue_empty():
    session, agent, _room_, h = _build()
    await h.on_say(_pkt({"text": "Fertig.", "seq": 1}))
    session.handles[0].finish()
    await agent.on_user_turn_completed(MagicMock(), new_message=MagicMock())  # kein Raise


# ── 4. overwrite ersetzt Queue und Rest; append hängt an ───────────────────────────
@pytest.mark.asyncio
async def test_overwrite_replaces_queue_and_rest():
    session, _agent, room, h = _build()
    session.user("speaking")
    await h.on_say(_pkt({"text": "A.", "seq": 1}))
    await h.on_say(_pkt({"text": "B.", "seq": 2, "mode": "append"}))
    await h.on_say(_pkt({"text": "A und B zusammen.", "seq": 3, "mode": "overwrite"}))
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    assert session.said == ["A und B zusammen."]
    await _drain()
    st = _status(room)
    assert (1, "replaced") in st and (2, "replaced") in st and (3, "queued") in st


@pytest.mark.asyncio
async def test_overwrite_drops_requeued_rest():
    session, _agent, _room_, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.user("speaking")
    session.handles[0].finish("Der Fix ist gebaut", interrupted=True)   # Rest in der Queue
    await h.on_say(_pkt({"text": "Neu zusammengefasst.", "seq": 2, "mode": "overwrite"}))
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    assert session.said == [LONG, "Neu zusammengefasst."]


@pytest.mark.asyncio
async def test_append_is_serial_and_in_order_after_requeue():
    session, _agent, _room_, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    await h.on_say(_pkt({"text": "Danach das.", "seq": 2, "mode": "append"}))
    assert session.said == [LONG]                        # nur eine Ausgabe bei livekit
    session.handles[0].finish("Der Fix ist gebaut und getestet.", interrupted=True)
    await asyncio.sleep(QUIET * 3)
    session.handles[-1].finish()
    await asyncio.sleep(QUIET * 3)
    assert session.said[1:] == ["Der Pull Request wartet auf deine Freigabe.", "Danach das."]


def _revises(room: MagicMock) -> list[dict]:
    return [json.loads(c.kwargs["payload"].decode())
            for c in room.local_participant.publish_data.await_args_list
            if c.kwargs.get("topic") == "operator.revise"]


@pytest.mark.asyncio
async def test_new_say_while_rest_only_waits_is_appended_without_revise():
    """Seit integ/r7: wartet der Rest nur (requeued, spricht nicht), hängt eine neue say
    ohne revise-Runde an (E2E 02.10.: 4 von 11 says liefen unnötig über revise)."""
    session, _agent, room, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.user("speaking")
    session.handles[0].finish("Der Fix ist gebaut und getestet.", interrupted=True)
    await h.on_say(_pkt({"text": "Noch was.", "seq": 2}))
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    session.handles[-1].finish()
    await asyncio.sleep(QUIET * 3)
    assert _revises(room) == []
    assert session.said[1:] == ["Der Pull Request wartet auf deine Freigabe.", "Noch was."]
    assert (1, "replaced") not in _status(room)


@pytest.mark.asyncio
async def test_new_say_while_first_only_queued_is_appended():
    session, _agent, room, h = _build()
    session.user("speaking")                              # Nutzer spricht: A wartet
    await h.on_say(_pkt({"text": "A.", "seq": 1}))
    await h.on_say(_pkt({"text": "B.", "seq": 2}))
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    session.handles[0].finish()
    await asyncio.sleep(QUIET * 3)
    session.handles[1].finish()
    await _drain()
    assert session.said == ["A.", "B."] and _revises(room) == []
    assert _status(room).count((1, "spoken")) == 1 and _status(room).count((2, "spoken")) == 1


@pytest.mark.asyncio
async def test_revise_only_when_replacing_mid_speech():
    session, _agent, room, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))        # spricht gerade
    session.handles[0].cut_at = "Der Fix ist gebaut und getestet."
    await h.on_say(_pkt({"text": "Noch was.", "seq": 2}))
    await _drain()
    rev = _revises(room)
    assert rev[-1]["unspoken"] == ["Der Pull Request wartet auf deine Freigabe."]
    assert (1, "replaced") in _status(room)


@pytest.mark.asyncio
async def test_overwrite_after_revise_keeps_newer_say():
    """Variante A: overwrite als Antwort auf operator.revise ersetzt nur, was beim revise
    offen war. Eine say, die danach kam, bleibt und spricht nach der Zusammenfassung."""
    session, _agent, room, h = _build()
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].cut_at = "Der Fix ist gebaut und getestet."
    await h.on_say(_pkt({"text": "Noch was.", "seq": 2}))          # -> revise, 2 gehalten
    session.user("speaking")
    await h.on_say(_pkt({"text": "Und das Neue.", "seq": 3}))      # während revise: anhängen
    await h.on_say(_pkt({"text": "PR wartet. Noch was.", "seq": 4, "mode": "overwrite"}))
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    session.handles[-1].finish()
    await asyncio.sleep(QUIET * 3)
    session.handles[-1].finish()
    await _drain()
    assert session.said[1:] == ["PR wartet. Noch was.", "Und das Neue."]
    st = _status(room)
    assert (2, "replaced") in st and (3, "spoken") in st and (4, "spoken") in st
    assert (3, "replaced") not in st and len(_revises(room)) == 1


@pytest.mark.asyncio
async def test_hold_timeout_speaks_held_before_newer_say():
    session, _agent, room, h = _build()
    h2 = build_relay_handlers(session, _agent, room=room, say_quiet=QUIET, hold_s=0.05)
    await h2.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].cut_at = "Der Fix ist gebaut und getestet."
    await h2.on_say(_pkt({"text": "Noch was.", "seq": 2}))         # revise, gehalten
    session.user("speaking")
    await h2.on_say(_pkt({"text": "Und das Neue.", "seq": 3}))
    await asyncio.sleep(0.1)                                        # Hold-Frist um
    session.user("listening")
    await asyncio.sleep(QUIET * 3)
    session.handles[-1].finish()
    await asyncio.sleep(QUIET * 3)
    assert session.said[1:] == ["Noch was.", "Und das Neue."]


# ── operator.interrupt: kein Nachsprechen ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_operator_interrupt_is_not_requeued():
    session, _agent, room, h = _build()
    s0 = session
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    s0.handles[0].cut_at = "Der Fix ist gebaut"
    await h.on_interrupt(_pkt({}))
    await asyncio.sleep(QUIET * 3)
    assert session.said == [LONG]
    assert (1, "interrupted") in _status(room)


# ── 5. Live-Modus: kein Nachsprechen nach Nutzer-Abbruch (Raum vivid-orbit-fresh-V32N) ─
@pytest.mark.asyncio
async def test_live_interrupted_say_is_not_repeated():
    session, _agent, room, h = _build(live=True)
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("Der Fix", interrupted=True)  # Transkript hinkt dem Audio nach
    await asyncio.sleep(QUIET * 3)
    # nichts doppelt (Live verpackt die Aussage in LIVE_SAY_USER, daher "in")
    assert len(session.said) == 1 and LONG in session.said[0]
    assert _status(room) == [(1, "queued"), (1, "interrupted")]


@pytest.mark.asyncio
async def test_live_next_say_plays_after_interrupt():
    # Positivkontrolle: die Queue läuft in Live weiter, nur der abgebrochene Satz nicht
    session, _agent, room, h = _build(live=True)
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("Der Fix", interrupted=True)
    await asyncio.sleep(QUIET * 3)
    # zweite Aussage erst nach dem Abbruch: kommt sie während seq 1 läuft, hält die
    # Revise-Logik sie zurück (hold_s=30), das ist ein anderer Pfad
    await h.on_say(_pkt({"text": "Zweite Aussage.", "seq": 2}))
    await asyncio.sleep(QUIET * 3)
    assert len(session.said) == 2 and "Zweite Aussage." in session.said[-1]
    assert LONG not in session.said[-1]


@pytest.mark.asyncio
async def test_pipeline_interrupted_say_still_requeued():
    # Positivkontrolle: Pipeline-Modus spricht den Rest weiter nach (unverändert)
    session, _agent, room, h = _build(live=False)
    await h.on_say(_pkt({"text": LONG, "seq": 1}))
    session.handles[0].finish("Der Fix ist gebaut und getestet.", interrupted=True)
    await asyncio.sleep(QUIET * 3)
    assert session.said[-1] == "Der Pull Request wartet auf deine Freigabe."
    assert (1, "requeued") in _status(room)


@pytest.mark.asyncio
async def test_say_without_seq_gets_own_seq():
    session, _agent, room, h = _build()
    await h.on_say(_pkt({"text": "Ohne seq."}))
    await _drain()
    assert _status(room)[0][0] == "vh-1"
