"""Preislogik (Oliver 01.10.2026).

Preis = echte Anbieterkosten x Faktor, MwSt obendrauf.
    Normal (STT -> LLM -> TTS): Faktor 3     VOICEHOOK_PRICE_FACTOR_NORMAL
    Live (Gemini Live):         Faktor 1,5   VOICEHOOK_PRICE_FACTOR_LIVE
    MwSt:                       19 %         VOICEHOOK_VAT_RATE (0.19)
    USD -> EUR:                 0.8807       VOICEHOOK_USD_EUR

Die Anbieterkosten kommen aus den Metriken des Workers in USD (live.metric_cost_usd,
Preise dort mit Quelle). Der Kurs-Default ist der EZB-Referenzkurs vom 30.09.2026
(1 EUR = 1,1355 USD -> 1 USD = 0,8807 EUR,
https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml). OFFENE ENTSCHEIDUNG:
fester Env-Kurs vs. täglicher Abruf; bis dahin per Env pflegen.

Einheit im Ledger: Mikro-Euro (1 µEUR = 0,000001 EUR) als Ganzzahl. v3 rechnete in
Credits zu 0,0001 EUR; das ist für Einzelmetriken zu grob (1 s STT kostet brutto
~0,0004 EUR). Das Guthaben ist ein Brutto-Betrag (das, was gezahlt wurde), deshalb
wird jede Abbuchung inklusive MwSt gerechnet.

Kaputte oder nicht positive Env-Werte fallen auf den Default zurück, nie auf 0
(ein Faktor 0 hieße: Gespräche kostenlos).
"""

from __future__ import annotations

import os

DEFAULT_FACTOR_NORMAL = 3.0
DEFAULT_FACTOR_LIVE = 1.5
DEFAULT_VAT_RATE = 0.19
DEFAULT_USD_EUR = 0.8807
USD_EUR_SOURCE = "EZB-Referenzkurs 30.09.2026 (1 EUR = 1,1355 USD)"

UEUR_PER_EUR = 1_000_000

DEFAULT_TOPUP_AMOUNTS_EUR = (5, 10, 20, 50)
MIN_TOPUP_EUR = 5       # Oliver 02.10.: Mindestaufladung 5 EUR (Env kann nur höher)
DEFAULT_MAX_TOPUP_EUR = 200


def _env_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    if v < 0 or (v == 0 and not allow_zero):
        return default
    return v


def factor(mode: str) -> float:
    if mode == "live":
        return _env_float("VOICEHOOK_PRICE_FACTOR_LIVE", DEFAULT_FACTOR_LIVE)
    return _env_float("VOICEHOOK_PRICE_FACTOR_NORMAL", DEFAULT_FACTOR_NORMAL)


def vat_rate() -> float:
    return _env_float("VOICEHOOK_VAT_RATE", DEFAULT_VAT_RATE, allow_zero=True)


def usd_eur() -> float:
    return _env_float("VOICEHOOK_USD_EUR", DEFAULT_USD_EUR)


def charge_ueur(usd: float, mode: str) -> int:
    """Brutto-Abbuchung in µEUR für Anbieterkosten `usd` im Modus `mode`."""
    if usd <= 0:
        return 0
    eur = usd * usd_eur() * factor(mode) * (1 + vat_rate())
    return max(1, round(eur * UEUR_PER_EUR))


def real_ueur(usd: float) -> int:
    """Echte Anbieterkosten in µEUR (ohne Faktor, ohne MwSt): usd x USD_EUR."""
    if usd <= 0:
        return 0
    return max(1, round(usd * usd_eur() * UEUR_PER_EUR))


def eur_to_ueur(eur: float) -> int:
    return round(eur * UEUR_PER_EUR)


def ueur_to_eur(ueur: int) -> float:
    return round(ueur / UEUR_PER_EUR, 4)


def min_topup_eur() -> int:
    try:
        v = int(os.environ.get("VOICEHOOK_TOPUP_MIN_EUR", "") or MIN_TOPUP_EUR)
    except ValueError:
        return MIN_TOPUP_EUR
    return max(MIN_TOPUP_EUR, v)


def max_topup_eur() -> int:
    try:
        v = int(os.environ.get("VOICEHOOK_TOPUP_MAX_EUR", "") or DEFAULT_MAX_TOPUP_EUR)
    except ValueError:
        return DEFAULT_MAX_TOPUP_EUR
    return max(min_topup_eur(), v)


def topup_amounts_eur() -> list[int]:
    """Vorschlagsbeträge (Env VOICEHOOK_TOPUP_AMOUNTS_EUR="5,10,20,50"), im erlaubten Bereich."""
    raw = os.environ.get("VOICEHOOK_TOPUP_AMOUNTS_EUR", "")
    try:
        vals = [int(x) for x in raw.split(",") if x.strip()] if raw.strip() else []
    except ValueError:
        vals = []
    lo, hi = min_topup_eur(), max_topup_eur()
    vals = sorted({v for v in vals if lo <= v <= hi})
    return vals or [v for v in DEFAULT_TOPUP_AMOUNTS_EUR if lo <= v <= hi] or [lo]
