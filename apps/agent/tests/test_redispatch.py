"""Re-Dispatch nach Worker-Neustart (Vorfall 02.10. 09:26 UTC, drift-calm-signal-UNVK)."""

from __future__ import annotations

from agent import redispatch
from agent.redispatch import GRACE_S, MAX_ATTEMPTS, MIN_SPACING_S, Redispatcher, needs_delta

HUMAN = {"identity": "host-cf9hmy", "kind": "STANDARD"}
DELTA = {"identity": "agent-AJ_x", "kind": "AGENT"}
CLAUDE = {"identity": "claude-crown-eb67", "kind": "STANDARD", "attributes": {"vh.role": "agent"}}


def test_needs_delta():
    assert needs_delta([HUMAN])                     # Oliver allein
    assert needs_delta([HUMAN, CLAUDE])             # Operator ersetzt Delta nicht
    assert not needs_delta([HUMAN, DELTA])          # Delta da
    assert not needs_delta([CLAUDE])                # kein Mensch: nichts
    assert not needs_delta([])
    assert needs_delta([{"identity": "h", "kind": 0}])  # twirp liefert kind teils als Zahl
    assert not needs_delta([HUMAN, {"identity": "a", "kind": 4}])


class _Box:
    def __init__(self, rooms, modes):
        self.rooms, self.modes, self.calls, self.t = rooms, modes, [], 0.0

    def rd(self):
        return Redispatcher(list_rooms=lambda: list(self.rooms), list_participants=lambda r: self.rooms[r],
                            dispatch=lambda r, a: self.calls.append((r, a)), mode_of=self.modes.get,
                            live_agent="voice-ai-live", clock=lambda: self.t)


def test_redispatch_normal_and_live_after_grace():
    box = _Box({"n1": [HUMAN], "l1": [HUMAN, CLAUDE], "ok": [HUMAN, DELTA], "x": [HUMAN]},
               {"n1": "normal", "l1": "live", "ok": "normal"})  # x: unbekannt -> nie
    rd = box.rd()
    assert rd.scan() == []                          # gerade erst ohne Delta: Karenz
    box.t += GRACE_S
    assert sorted(rd.scan()) == [("l1", "voice-ai-live"), ("n1", "voice-ai")]
    assert ("ok", "voice-ai") not in box.calls and all(r != "x" for r, _ in box.calls)


def test_attempts_bounded_and_reset_on_success():
    box = _Box({"n1": [HUMAN]}, {"n1": "normal"})
    rd = box.rd()
    rd.scan()
    for _ in range(20):
        box.t += GRACE_S + MIN_SPACING_S
        rd.scan()
    assert len(box.calls) == MAX_ATTEMPTS           # gibt auf, Frontend beendet (PR #126)
    box.rooms["n1"] = [HUMAN, DELTA]                # Delta kam doch
    rd.scan()
    box.rooms["n1"] = [HUMAN]                       # nächster Neustart
    box.t += 1
    rd.scan()
    box.t += GRACE_S
    rd.scan()
    assert len(box.calls) == MAX_ATTEMPTS + 1       # Positivkontrolle: wieder besetzt


def test_spacing_between_attempts():
    box = _Box({"n1": [HUMAN]}, {"n1": "normal"})
    rd = box.rd()
    rd.scan()
    box.t += GRACE_S
    rd.scan()
    box.t += 3
    rd.scan()
    assert len(box.calls) == 1


def test_room_mode_from_storage(monkeypatch):
    from agent import freetier
    from agent.billing import db

    monkeypatch.setattr(db, "room_binding", lambda r: {"w": ("acc", "live", "active"),
                                                      "c": ("acc", "normal", "closed")}.get(r))
    monkeypatch.setattr(freetier, "room_keys", lambda r: {"f": ("normal", ["k"])}.get(r))
    assert redispatch.room_mode("w") == "live"
    assert redispatch.room_mode("c") is None        # beendete Bindung: nie neu besetzen
    assert redispatch.room_mode("f") == "normal"
    assert redispatch.room_mode("unbekannt") is None


def test_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    assert redispatch.start_background() is None
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    monkeypatch.setenv("VOICEHOOK_REDISPATCH", "0")
    assert redispatch.start_background() is None
