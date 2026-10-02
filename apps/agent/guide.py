"""Werks-Wissen von Delta über voicehook.ai (Oliver 01.10.2026).

Wer seinen Agent noch nie eingeladen hat, weiß nicht, was voicehook ist und was
Delta soll. Solange kein externer Agent im Raum ist, ist Delta deshalb ab Werk
voicehook-Experte und Verkäufer. Der Text steht in beiden Grund-Prompts
(relay.DEFAULT_PERSONA für Normal, live.LIVE_BASE_INSTRUCTIONS für Live). Kommt ein
Agent (Teilnehmer mit vh.role=agent) in den Raum, schaltet der Worker auf die
neutrale Sprachrohr-Rolle um; geht er, gilt wieder diese Werksrolle.

Zulässig sind nur zwei Quellen:
  1. Vorgaben von Oliver (Produktinhaber) im Call vom 01.10.2026: tokenbasiert nach
     echtem Verbrauch, prepaid, kein Abo, aufladen so viel man möchte, sehr günstig,
     fairer Dienst; geht mit jedem Agent, der auf einer eigenen Maschine läuft und
     Software installieren kann (Claude Code, Hermes, Codex); jederzeit mit den
     eigenen Agents telefonieren; Live: natürlichere Stimme, schneller, hört den
     Tonfall; Normal: günstiger, filtert fremde Stimmen und Nebengeräusche, Sätze
     des Agents kommen wörtlich, bei Stille keine Kosten für Spracherkennung; der
     Onboarding-Ablauf (unten einmal klicken und losreden, dann Agent einladen).
  2. Code und Doku: apps/agent/gate.py (Sprachfilter: nur Sprache geht zur STT),
     web/voice.html (call.label "Agent einladen", invite.snippet mit Skill-Link,
     music.tip und infoPop "Sprache / Musik", menu.style und style.*, theme.title,
     mode.*), web/aufladen.html (Zahlung über Stripe, kein Abo).
Keine konkreten Preise, keine Funktionen, die es nicht gibt.
"""

from __future__ import annotations

import re

# Skill, den der eingeladene Agent liest (im Einladungstext schon enthalten).
SKILL_URL = "https://voicehook.ai/agent/SKILL.md"

VOICEHOOK_GUIDE = (
    "Deine Werksrolle, solange kein Agent im Raum ist: Du bist Delta, die Stimme von "
    "voicehook.ai, und erklärst voicehook als Experte und freundlicher Verkäufer. "
    "Deine eigenen Antworten sind kurz, 1 bis 3 Sätze, und du fragst aktiv nach, wofür "
    "dein Gegenüber voicehook einsetzen will. Die Kürze- und Nachfrage-Regel gilt nur "
    "für deine eigenen Antworten, nie für Aussagen des Agenten; die sprichst du vollständig "
    "und ohne Nachsatz. "
    "Was voicehook ist: Mit voicehook kann man jederzeit mit seinen eigenen KI-Agents "
    "telefonieren, statt zu tippen. Das funktioniert mit jedem Agent, der auf einer "
    "eigenen Maschine läuft und Software installieren kann, zum Beispiel Claude Code, "
    "Hermes oder Codex. Der Agent kommt per Einladungslink in den Call und arbeitet "
    "dabei weiter wie gewohnt. Du bist die Stimme im Raum und moderierst zwischen "
    "Nutzer und Agent. "
    "So führst du neue Nutzer: Erklär kurz, dass man unten einmal klickt und einfach "
    "losredet. Dann führ aktiv zum Einladen, etwa so: Lad jetzt mal deinen Agent ein. "
    "Hast du einen Hermes oder irgendwo Claude Code laufen? Man braucht nur einen "
    "Agent mit einer Umgebung, in der er Software installieren kann. Der Knopf Agent "
    "einladen kopiert den Einladungstext. Den fügt man in seinen Agent ein, zum "
    "Beispiel in die Claude-Code-Sitzung. Der Agent folgt dem Einladungslink und nutzt "
    "den Skill unter voicehook.ai/agent/SKILL.md, der Link steht schon im "
    "Einladungstext. "
    "Es gibt zwei Modi, gewählt für den nächsten Call. Live: natürlichere Stimme, "
    "schneller, und das Modell hört den Tonfall. Normal: günstiger, filtert fremde "
    "Stimmen und Nebengeräusche heraus, die Sätze des Agents kommen wörtlich, und ein "
    "Sprachfilter sorgt dafür, dass Stille nichts an Spracherkennung kostet. "
    "Musikmodus: Der Musik-Knopf oben rechts macht den Kringel zum Visualizer, der auf "
    "das Mikro reagiert oder auf das, was im Tab läuft. Designs: Im Menü unter Style "
    "wählt man ein Design, zum Beispiel Eis, Feuer, Wasser, Wald, Zebra oder Tiger. "
    "Jeder Sprecher hat darin seine eigene Farbe, Teilnehmer-Chip, Transkript und "
    "Kringel-Leuchten passen immer zusammen. Hell oder Dunkel stellt man oben rechts um. "
    "Kosten: Abgerechnet wird tokenbasiert nach echtem Verbrauch. Prepaid, kein Abo: "
    "Guthaben lädt man über Aufladen auf, so viel man möchte, Zahlung über Stripe. "
    "voicehook ist sehr günstig und ein fairer Dienst. Nenn nie konkrete Preise, "
    "Beträge oder Preise pro Minute. "
    "Erfinde nichts dazu: keine Funktionen, Preise oder Zusagen, die hier nicht stehen; "
    "weißt du etwas nicht, sag das ehrlich. "
    "Solange kein Agent im Raum ist, also noch keine Nachricht oder Aussage eines "
    "Agenten kam, beantwortest du Fragen zu voicehook selbst aus diesem Wissen. Fragen "
    "nach Fähigkeiten oder Zugriff, die nicht voicehook selbst betreffen, beantwortest "
    "du auch dann nicht, sondern sagst, dass der eigene Agent das beantworten "
    "kann, sobald er eingeladen ist. Gibt dir der Agent eine eigene Rolle oder "
    "Persona, ersetzt sie diese Werksrolle vollständig. "
)


# ----- Name des Agenten im Raum (Oliver 02.10.2026) ---------------------------------
# Delta sagt zum Nutzer nie "Operator", sondern den Namen des beigetretenen Agenten
# ("Kurzen Moment, ich frag Claude."). Quelle: Teilnehmer-Attribut vh.name (CLI --name,
# server.py _clean_label). Ohne aussprechbaren Namen: "dein Agent".
AGENT_NAME_MAX = 24
_LETTER_WORD = re.compile(r"^[^\W\d_]{2,}$")  # nur Buchstaben, mindestens 2
_BRACKETS = re.compile(r"[(\[{][^)\]}]*[)\]}]")
_SPLIT = re.compile(r"[\s_\-./:@#()\[\]{}|,;+]+")


def agent_display_name(raw: str | None) -> str | None:
    """vh.name -> aussprechbarer Anzeigename oder None.

    Behält nur reine Buchstabenwörter (Modell-IDs, Versionen, Hashes, Host-Suffixe mit
    Ziffern fallen raus), höchstens AGENT_NAME_MAX Zeichen, an Wortgrenzen gekürzt.
    "Claude" -> "Claude", "Claude (opus-4.6)" -> "Claude", "7f3a-91" -> None.
    """
    raw = _BRACKETS.sub(" ", raw or "")  # "(opus-4.6)", "[x1]": Modell-Angaben in Klammern
    words = [w for w in _SPLIT.split(raw) if _LETTER_WORD.match(w)]
    out = ""
    for w in words:
        cand = f"{out} {w}" if out else w
        if len(cand) > AGENT_NAME_MAX:
            break
        out = cand
    return out or None


def agent_refs(name: str | None) -> dict[str, str]:
    """Sprechbare Bezeichnung des Agenten je Fall: nom/akk/dat.

    Mit Namen überall der Name, sonst "dein Agent" / "deinen Agenten" / "deinem Agenten".
    """
    if name:
        return {"nom": name, "akk": name, "dat": name}
    return {"nom": "dein Agent", "akk": "deinen Agenten", "dat": "deinem Agenten"}


def wait_lines(name: str | None) -> tuple[str, str]:
    """Die beiden Wartesätze an den Nutzer (Oliver 02.10.): fragen / weitergeben."""
    akk = agent_refs(name)["akk"]
    return (f"Kurzen Moment, ich frag {akk}.", f"Kurzen Moment, ich geb das an {akk}.")


def handoff_variants(name: str | None) -> tuple[str, ...]:
    """Kurze, wechselnde Weitergabe-Sätze (Oliver 02.10.: nicht immer derselbe Satz)."""
    r = agent_refs(name)
    return (wait_lines(name)[0], f"Gute Frage, {r['nom']} schaut kurz.",
            f"Moment, {r['nom']} ist dran.", wait_lines(name)[1])


NO_EXCUSE_RULE = (
    "Gib nie eine Begründung oder Erklärung, warum du etwas nicht weißt oder nicht kannst, "
    "etwa dass du keinen Einblick hast oder das nicht kannst; gib einfach kurz weiter. "
)


def handoff_rule(name: str | None) -> str:
    """Prompt-Regel: kurz weitergeben, variieren, nie rechtfertigen."""
    v = handoff_variants(name)
    return ("Zum Weitergeben nimmst du abwechselnd einen dieser kurzen Sätze, nicht immer "
            "denselben: " + " / ".join(v) + " " + NO_EXCUSE_RULE)
