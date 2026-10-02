"""Gemini-3.8-Live-Testmodus (eigener Worker `voice-ai-live`, VOICEHOOK_PIPELINE=live).

Statt STT -> LLM -> TTS spricht ein Realtime-Modell selbst (Audio rein, Audio raus).
operator.say wird dort zur Anweisung an Gemini, nicht zu TTS: Gemini formuliert
selbst (Live-Qualität), muss den Inhalt aber vollständig und unverfälscht
übernehmen; bei "wörtlich"/"eins zu eins"/Transkript/Zitat exakt Wort für Wort
(live_say_user_input). Wortgleichheit ist so NICHT garantiert, nur angewiesen.

Kosten: Die Live API rechnet pro Turn den GESAMTEN Kontext neu ab (Google, Live API
best practices). Deshalb Kontext-Kompression: bei TRIGGER Tokens auf TARGET kürzen
(~25 Audio-Tokens/s -> 15k/7k ≈ 10/4,7 Min Audio-Gedächtnis, ~5 Min zwischen zwei
Kompressionen; L4, Oliver 02.10.2026). Langzeitwissen gehört
in die Persona (Text, billig), nicht in den Audio-Verlauf.
"""

from __future__ import annotations

import os
import re

from .board import board_block
from .clock import now_block
from .core import CORE_ANCHOR, MARK_AGENT, MARK_SYSTEM, compose, core_live, persona_block
from .guide import VOICEHOOK_GUIDE, agent_refs

DEFAULT_LIVE_MODEL = "gemini-3.8-live"
DEFAULT_LIVE_VOICE = "Charon"
# L4 (Oliver 02.10.2026): nicht ständig feuern, aber genug kürzen, dass es eine Weile
# reicht. (15000 - 7000) / ~25 Audio-Tokens/s ≈ 320 s ≈ 5 Min zwischen zwei
# Kompressionen. Kosten: jeder Turn rechnet den ganzen Kontext ab (32k wäre ~3x teurer).
DEFAULT_TRIGGER_TOKENS = 15000
DEFAULT_TARGET_TOKENS = 7000


def _env_tokens(*names: str) -> int | None:
    """Erster gesetzte Env-Wert als positive Ganzzahl; ungültig -> None."""
    for n in names:
        raw = os.environ.get(n)
        if raw is None or not raw.strip():
            continue
        try:
            v = int(raw)
        except ValueError:
            return None
        return v if v > 0 else None
    return None


def compress_tokens() -> tuple[int, int]:
    """(trigger, target) der Kontext-Kompression.

    Env VOICEHOOK_LIVE_COMPRESS_TRIGGER / _TARGET (alte Namen
    VOICEHOOK_LIVE_TRIGGER_TOKENS / _TARGET_TOKENS gelten weiter). Kaputt, <= 0 oder
    target >= trigger -> beide Defaults (nie eine Kompression, die nichts kürzt)."""
    trigger = _env_tokens("VOICEHOOK_LIVE_COMPRESS_TRIGGER", "VOICEHOOK_LIVE_TRIGGER_TOKENS")
    target = _env_tokens("VOICEHOOK_LIVE_COMPRESS_TARGET", "VOICEHOOK_LIVE_TARGET_TOKENS")
    trigger = DEFAULT_TRIGGER_TOKENS if trigger is None else trigger
    target = DEFAULT_TARGET_TOKENS if target is None else target
    if target >= trigger:
        return DEFAULT_TRIGGER_TOKENS, DEFAULT_TARGET_TOKENS
    return trigger, target

# Echte System-Instruktion des Live-Workers (geht nur beim Verbindungsaufbau an
# Gemini): fester Delta-Kern (core.py) zuerst, dort nicht austauschbar. Persona,
# Status und Agentenaussagen kommen später als markierte User-Turns: das
# Google-Plugin schickt update_instructions()/instructions= als role="model"-Turn,
# Gemini hält sie dann für eigene Aussagen (livekit/agents PR #5049, Issue #5496;
# realtime_api.py 1.8.3 Z. 646-672, 869).
# Markierungen: [Agent] = Aussage des Agenten, [System] = Vorgabe. Das Wort
# "Operator" steht nicht mehr in den Turns (Priming, Oliver 02.10.2026).
def live_core_instructions(name: str | None = None, user: str | None = None) -> str:
    """Kern Live ohne Werksrolle (neutrale Sprachrohr-Regeln)."""
    return core_live(name, user)


LIVE_CORE_INSTRUCTIONS = live_core_instructions()
# Kern + Werksrolle + Anker: System-Instruktion beim Verbindungsaufbau.
LIVE_BASE_INSTRUCTIONS = compose(LIVE_CORE_INSTRUCTIONS, VOICEHOOK_GUIDE)


# Datum/Uhrzeit (clock.py, Oliver 02.10.2026). Die System-Instruktion geht nur beim
# Verbindungsaufbau an Gemini, ein Zeit-Turn je Nutzer-Satz würde den ganzen Kontext
# pro Turn neu abrechnen (Kosten, siehe oben) und Gemini könnte ihn kommentieren.
# Deshalb: Zeit in der Start-Instruktion (eigene Schicht hinter Kern und Guide) und
# huckepack im Status-Turn (operator.status), den es ohnehin gibt und der den alten
# ersetzt. Laufende Calls mit Agent bekommen so mit jedem Board-Update (Rate-Limit
# 5 s) die aktuelle Zeit, ohne einen einzigen zusätzlichen Turn.
def live_base_instructions(now=None) -> str:  # noqa: ANN001
    """System-Instruktion beim Sessionstart: Kern + Werksrolle + Zeitblock + Anker."""
    return compose(LIVE_CORE_INSTRUCTIONS, VOICEHOOK_GUIDE, now_block(now))


def live_stamp(turn: str, now=None) -> str:  # noqa: ANN001
    """Zeitblock ans Ende eines [System]-Turns (Status-Turn; Turn bleibt sonst gleich)."""
    return f"{turn.rstrip()}\n{now_block(now)}"
_SILENT = " Nicht vorlesen, nicht darauf antworten."


# Werksrolle aus/an, wenn ein externer Agent (vh.role=agent) kommt oder geht. Die
# System-Instruktion lässt sich mitten in der Session nicht sauber tauschen
# (realtime_api.py 1.8.3 Z. 646-675), deshalb ein markierter User-Turn
# (update_chat_ctx, Z. 677ff); der Chat-Kontext wird bei einem Reconnect wieder
# eingespielt (Z. 995-1020). Der Kern gilt weiter und wird mit Namen wiederholt.
def live_agent_joined_user(name: str | None = None, user: str | None = None) -> str:
    nom = agent_refs(name)["nom"]
    return (
        f"{MARK_SYSTEM} {nom[0].upper() + nom[1:]} ist jetzt im Raum. Deine Werksrolle als "
        "voicehook-Experte und Verkäufer gilt ab sofort nicht mehr, keine Verkaufssätze. "
        f"Die Regeln gelten weiter, mit {nom} als Agent:{_SILENT}\n"
        + live_core_instructions(name, user)
    )


LIVE_AGENT_JOINED_USER = live_agent_joined_user()


def live_status_user(board: dict | None, name: str | None = None) -> str:
    """Board als markierter User-Turn. Gemini Live kann Turns nicht löschen
    (realtime_api.py 1.8.3 _sync_chat_ctx: "does not support removing messages");
    der Worker entfernt den alten Status-Turn deshalb aus dem lokalen Chat-Kontext
    (konstant, Reconnect spielt nur den letzten ein) und dieser Text erklärt jeden
    früheren Stand für ungültig. Server-seitig bleibt je Update ein kurzer Turn
    (Rate-Limit 5 s, Budget board.board_budget() Zeichen, Kontext-Kompression räumt ab)."""
    nom = agent_refs(name)["nom"]
    block = board_block(board, nom).strip()
    if not block:
        who = nom[0].upper() + nom[1:]
        block = f"Es gibt keinen aktuellen Stand von {who}, frühere Stände gelten nicht mehr."
    return f"{MARK_SYSTEM} " + block + _SILENT


LIVE_AGENT_LEFT_USER = (
    f"{MARK_SYSTEM} Der Agent hat den Raum verlassen. Ab sofort gilt wieder deine Werksrolle "
    "als voicehook-Experte statt des Wissens, das dir der Agent gegeben hat. Die Regeln ganz "
    f"oben gelten weiter.{_SILENT} " + VOICEHOOK_GUIDE
)
# Normalfall: Gemini darf natürlich formulieren, der Inhalt bleibt exakt derselbe.
LIVE_SAY_USER = (
    f"{MARK_AGENT} Sprich jetzt diese Aussage. Übernimm ihren Inhalt vollständig: nichts "
    "hinzufügen, keine eigenen Behauptungen oder Fakten, nichts weglassen, nichts "
    "abschwächen oder umdeuten, keine Einleitung, kein Nachsatz. Nur die Formulierung "
    "darf gesprochen natürlich klingen. Aussage: «{text}»"
)
# Wörtlich: ausdrücklich verlangt, Transkript-Text oder Zitat.
LIVE_SAY_VERBATIM_USER = (
    f"{MARK_AGENT} Wörtlich. Sprich jetzt exakt diesen Text, Wort für Wort, ohne jede "
    "Änderung, ohne Einleitung, ohne Zusatz und ohne Nachsatz: «{text}»"
)
# Führende Markierung, mit der der Agent Wortgleichheit verlangt; wird nicht mitgesprochen.
_VERBATIM_PREFIX = re.compile(
    r"^\s*(?:wörtlich|woertlich|eins\s+zu\s+eins|1\s*:\s*1|1\s+zu\s+1)\s*:\s*", re.IGNORECASE
)
# Inhalte, die nie umformuliert werden dürfen: Transkript-Text und Zitate.
_VERBATIM_CONTENT = re.compile(r"transkript|zitat|[„“”\"«»]", re.IGNORECASE)


def live_say_user_input(text: str) -> str:
    """User-Turn für ein operator.say im Live-Modus.

    Code entscheidet, ob wörtlich (nicht das Modell): führende Markierung
    "wörtlich:"/"eins zu eins:"/"1:1:" (wird entfernt), Transkript-Text oder Zitat.
    """
    m = _VERBATIM_PREFIX.match(text)
    if m and text[m.end():].strip():
        return LIVE_SAY_VERBATIM_USER.format(text=text[m.end():].strip())
    if _VERBATIM_CONTENT.search(text):
        return LIVE_SAY_VERBATIM_USER.format(text=text)
    return LIVE_SAY_USER.format(text=text)


def live_say_rest_user_input(text: str, spoken: str) -> str:
    """operator.say, das der Nutzer unterbrochen hat (relay.py Nachsprechen): Gemini
    formuliert um, der ungesprochene Rest ist am Text nicht abzulesen. Deshalb die ganze
    Aussage plus das schon Gesagte: weiter ab dort, nichts wiederholen, nichts weglassen."""
    base = live_say_user_input(text)
    if not (spoken or "").strip():
        return base
    return (f"{base} Du wurdest dabei unterbrochen, gesagt hast du schon: «{spoken.strip()}». "
            "Sprich jetzt nur den noch fehlenden Rest, ohne Entschuldigung und ohne "
            "Wiederholung.")


def live_persona_user(text: str, name: str | None = None) -> str:
    """Bereinigte Persona als Wissens-Turn: ersetzt die Werksrolle, nie den Kern."""
    return (f"{MARK_SYSTEM} Ab sofort gilt statt deiner Werksrolle als voicehook-Experte "
            f"dieses Wissen.{_SILENT} " + persona_block(text, name) + " " + CORE_ANCHOR)


# Kompatibel: Vorlage mit {text}, Agent ohne Namen.
LIVE_PERSONA_USER = live_persona_user("{text}")

# Platzhalter, die Gemini statt echter Sprache als Transkript liefert
NO_SPEECH_MARKERS = ("<no speech detected>", "&lt;no speech detected&gt;")
# Varianten: "<no speech>{pause}" (Live 02.10.2026, Raum vivid-orbit-fresh-V32N),
# "<no speech detected>", HTML-escaped, "{pause}" allein. Nur Platzhalter, keine Wörter.
_NO_SPEECH_RE = re.compile(
    r"^(?:\s|<[^<>]*\bno\s*speech\b[^<>]*>|&lt;[^&]*\bno\s*speech\b[^&]*&gt;|\{[a-z_ ]*\})+$",
    re.IGNORECASE,
)


def is_no_speech(text: str) -> bool:
    """True, wenn das Transkript nur aus Gemini-Platzhaltern besteht (kein echtes Wort)."""
    t = (text or "").strip()
    return bool(t) and (t in NO_SPEECH_MARKERS or bool(_NO_SPEECH_RE.match(t)))

# Stand aller Preise in dieser Datei (live auf den offiziellen Preisseiten geprüft)
PRICES_AS_OF = "2026-09-30"

# USD je 1M Tokens, Gemini 3.8 Live, Standard Paid Tier:
# https://ai.google.dev/gemini-api/docs/pricing (geprüft 30.09.2026)
# Input 0,75 Text / 3,00 Audio; Output 4,50 Text / 12,00 Audio
PRICE_AUDIO_IN = 3.00
PRICE_TEXT_IN = 0.75
PRICE_AUDIO_OUT = 12.00
PRICE_TEXT_OUT = 4.50


def _realtime_model_cls():
    from livekit.plugins.google import realtime

    return realtime.RealtimeModel


def build_live_llm():
    from google.genai import types

    trigger, target = compress_tokens()
    return _realtime_model_cls()(
        model=os.environ.get("VOICEHOOK_LIVE_MODEL", DEFAULT_LIVE_MODEL),
        voice=os.environ.get("VOICEHOOK_LIVE_VOICE", DEFAULT_LIVE_VOICE),
        # KEIN language=: native-audio-Modelle unterstützen language_code nicht
        # (Google Live API capabilities); Deutsch steht in LIVE_BASE_INSTRUCTIONS.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=trigger,
            sliding_window=types.SlidingWindow(target_tokens=target),
        ),
        # Verbindung hält ~10 Min; Resumption hält die Sitzung über Reconnects.
        # KEIN transparent=True: nur Enterprise, die Developer API wirft ValueError
        # (live gegen gemini-3.8-live geprüft 30.09.2026).
        session_resumption=types.SessionResumptionConfig(),
    )


def _n(obj, name: str) -> int:
    return int(getattr(obj, name, 0) or 0) if obj is not None else 0


def live_cost_usd(m) -> float:
    """Kosten eines Turns aus RealtimeModelMetrics.

    Konservativ: gecachte und unaufgeschlüsselte Input-Tokens zählen als Audio-Input
    (teuerster Input-Preis), bis Google für Live einen Cache-Rabatt dokumentiert.
    """
    ind, outd = getattr(m, "input_token_details", None), getattr(m, "output_token_details", None)
    text_in = _n(ind, "text_tokens")
    audio_in = _n(m, "input_tokens") - text_in
    text_out = _n(outd, "text_tokens")
    audio_out = _n(m, "output_tokens") - text_out
    return (
        audio_in * PRICE_AUDIO_IN
        + text_in * PRICE_TEXT_IN
        + audio_out * PRICE_AUDIO_OUT
        + text_out * PRICE_TEXT_OUT
    ) / 1_000_000


# ---- Kostenanzeige im laufenden Call (beide Modi) ---------------------------
# Preise USD, alle geprüft am 30.09.2026:
# - Deepgram nova-3 Streaming Monolingual (Default language "de"), Pay As You Go:
#   regulär 0,0077 $/min, derzeit Aktion 0,0048 $/min; konservativ der reguläre.
#   https://deepgram.com/pricing
# - Google Cloud TTS Chirp 3: HD, 0,00003 $/Zeichen (30 $ je 1M Zeichen):
#   https://cloud.google.com/text-to-speech/pricing
# - gemini-2.5-flash Standard Paid Tier, Input Text 0,30 $, Output 2,50 $ je 1M Tokens:
#   https://ai.google.dev/gemini-api/docs/pricing
# Live: live_cost_usd().
PRICE_STT_PER_S = 0.0077 / 60
PRICE_TTS_PER_CHAR = 30.0 / 1_000_000
PRICE_LLM_IN = 0.30 / 1_000_000
PRICE_LLM_OUT = 2.50 / 1_000_000


def metric_cost_usd(m) -> float:
    """Kosten eines einzelnen metrics_collected-Eintrags (0 für unbekannte Typen)."""
    kind = type(m).__name__
    if kind == "RealtimeModelMetrics":
        return live_cost_usd(m)
    if kind == "STTMetrics":
        return float(getattr(m, "audio_duration", 0) or 0) * PRICE_STT_PER_S
    if kind == "TTSMetrics":
        return int(getattr(m, "characters_count", 0) or 0) * PRICE_TTS_PER_CHAR
    if kind == "LLMMetrics":
        return (int(getattr(m, "prompt_tokens", 0) or 0) * PRICE_LLM_IN
                + int(getattr(m, "completion_tokens", 0) or 0) * PRICE_LLM_OUT)
    return 0.0


# ---- Laufende Summe mit offengelegter Basis ---------------------------------
# Kosten = gemessene Menge aus den Metriken x geprüfter Preis. Die Basis nennt nur
# Mengen, die tatsächlich aus einer Metrik stammen: ein Feld erscheint erst, wenn
# eine Metrik dieses Typs eingegangen ist (Pipeline zeigt nie rt_*, Live nie stt_*).
_BASIS_FIELDS = {
    "STTMetrics": ("stt_audio_s",),
    "TTSMetrics": ("tts_chars",),
    "LLMMetrics": ("llm_in_tokens", "llm_out_tokens"),
    "RealtimeModelMetrics": (
        "rt_audio_in_tokens", "rt_audio_out_tokens", "rt_text_in_tokens", "rt_text_out_tokens",
    ),
}


def metric_basis(m) -> dict[str, float]:
    """Abgerechnete Mengen eines metrics_collected-Eintrags ({} für unbekannte Typen).

    rt_audio_in_tokens ist wie in live_cost_usd() alles Input außer Text (inkl.
    gecachter/unaufgeschlüsselter Tokens), damit Basis und Summe dieselbe Rechnung sind.
    """
    kind = type(m).__name__
    if kind == "RealtimeModelMetrics":
        ind, outd = getattr(m, "input_token_details", None), getattr(m, "output_token_details", None)
        text_in, text_out = _n(ind, "text_tokens"), _n(outd, "text_tokens")
        return {
            "rt_audio_in_tokens": _n(m, "input_tokens") - text_in,
            "rt_audio_out_tokens": _n(m, "output_tokens") - text_out,
            "rt_text_in_tokens": text_in,
            "rt_text_out_tokens": text_out,
        }
    if kind == "STTMetrics":
        return {"stt_audio_s": float(getattr(m, "audio_duration", 0) or 0)}
    if kind == "TTSMetrics":
        return {"tts_chars": int(getattr(m, "characters_count", 0) or 0)}
    if kind == "LLMMetrics":
        return {
            "llm_in_tokens": int(getattr(m, "prompt_tokens", 0) or 0),
            "llm_out_tokens": int(getattr(m, "completion_tokens", 0) or 0),
        }
    return {}


class CostMeter:
    """Summiert Kosten und Basis eines Calls; meldet nur bei geänderter Summe.

    Kein Takt: take_update() liefert nur dann eine Meldung, wenn sich der gerundete
    USD-Betrag seit der letzten Meldung geändert hat. Stille ohne Metriken erzeugt
    also nie eine Meldung.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.usd = 0.0
        self.basis: dict[str, float] = {}
        self._sent_usd = 0.0  # 0 USD wird nie gemeldet

    def add(self, m) -> float:
        """Metrik verbuchen; gibt die Kosten genau dieser Metrik zurück."""
        usd = metric_cost_usd(m)
        for key, value in metric_basis(m).items():
            self.basis[key] = self.basis.get(key, 0) + value
        self.usd += usd
        return usd

    def payload(self) -> dict:
        basis = {k: (round(v, 3) if k == "stt_audio_s" else int(v)) for k, v in self.basis.items()}
        return {"usd": round(self.usd, 5), "mode": self.mode, "basis": basis, "prices_as_of": PRICES_AS_OF}

    def take_update(self) -> dict | None:
        """Payload, wenn sich usd seit der letzten Meldung geändert hat, sonst None."""
        p = self.payload()
        if p["usd"] == self._sent_usd:
            return None
        self._sent_usd = p["usd"]
        return p
