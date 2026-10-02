"""Lebenszeichen des externen Agenten (operator.alive), Oliver 02.10.2026.

Die voicehook-agent CLI (ab 0.8.0) sendet auf Topic `operator.alive` alle 10 s
{"alive": true, "ts", "idle_s"}, solange ihr Brain `next`/`say` aktiv bedient, und
einmal {"alive": false} beim Verlassen. Verwaist (niemand bedient sie) sendet sie
nichts. Daraus folgt pro Raum:

- legacy:      Agent im Raum, hat noch nie ein Lebenszeichen geschickt (alte CLI)
               -> gilt als erreichbar, Verhalten wie bisher.
- erreichbar:  letztes alive:true höchstens ALIVE_STALE_S alt.
- unerreichbar: Agent im Raum, aber alive:false oder kein Lebenszeichen seit
               ALIVE_STALE_S. Delta sagt dann den festen Satz statt zu warten.
"""

from __future__ import annotations

from .core import MARK_SYSTEM
from .guide import agent_refs

TOPIC_ALIVE = "operator.alive"
ALIVE_STALE_S = 20.0  # 2 verpasste Lebenszeichen (10 s Takt) = hört nicht zu


def unreachable_sentence(name: str | None) -> str:
    """Fester Satz, nichts erfunden: "Claude ist gerade nicht erreichbar." """
    nom = agent_refs(name)["nom"]
    return f"{nom[:1].upper()}{nom[1:]} ist gerade nicht erreichbar."


class OperatorAlive:
    """Letztes Lebenszeichen des Agenten im Raum (eine Instanz je Worker-Job)."""

    def __init__(self, stale_s: float = ALIVE_STALE_S) -> None:
        self.stale_s = stale_s
        self.reset()

    def reset(self) -> None:
        self.seen = False
        self.last: float | None = None
        self.off = False

    def on_packet(self, data: dict, now: float) -> None:
        self.seen = True
        if data.get("alive") is False:
            self.off = True
            return
        self.off = False
        self.last = now

    def reachable(self, present: bool, now: float) -> bool:
        if not present:
            return False
        if not self.seen:
            return True  # alte CLI ohne Lebenszeichen: wie bisher
        if self.off or self.last is None:
            return False
        return now - self.last <= self.stale_s


def unreachable_block(name: str | None) -> str:
    """Prompt-Zusatz, solange der Agent nicht zuhört: fester Satz statt Wartesatz."""
    line = unreachable_sentence(name)
    return (
        f" WICHTIG, gilt bis auf Widerruf: {line} Statt jedes Wartesatzes, statt "
        f"Weitergeben und statt einer Stand-Auskunft sagst du genau diesen Satz: '{line}' "
        "Du wartest auf keine Antwort, versprichst nichts und erfindest keine Antwort. "
    )


def live_unreachable_user(name: str | None) -> str:
    return (MARK_SYSTEM + unreachable_block(name)
            + "Nicht vorlesen, nicht darauf antworten.")


def live_reachable_user(name: str | None) -> str:
    who = agent_refs(name)["nom"]
    return (f"{MARK_SYSTEM} {who[:1].upper()}{who[1:]} ist wieder erreichbar. Der Satz "
            f"'{unreachable_sentence(name)}' gilt nicht mehr, es gelten wieder die "
            "Wartesatz-Regeln. Nicht vorlesen, nicht darauf antworten.")
