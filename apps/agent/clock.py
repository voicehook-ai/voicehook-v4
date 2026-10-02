"""Datum und Uhrzeit für Delta (Oliver 02.10.2026: "Delta kennt Datum und Uhrzeit nicht").

Eigener kleiner Block, nie Teil des festen Kerns (core.py): der Kern bleibt
konstant, die Zeit kommt als eigene Schicht dazu.
  Normal: RelayAgent.llm_node setzt den Block bei JEDER Antwort neu (immer aktuell).
  Live:   System-Instruktion beim Sessionstart + jeder Status-Turn (operator.status).
          Begründung in live.py.

Zeitzone fest Europe/Berlin. Wochentag und Monat auf Deutsch ohne Locale (der
Server-Locale ist nicht garantiert). Fehlt die Zeitzonen-Datenbank, rechnet
_berlin_fallback die EU-Sommerzeitregel selbst.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

TZ_NAME = "Europe/Berlin"

_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
           "September", "Oktober", "November", "Dezember")


def _last_sunday_utc(year: int, month: int) -> datetime:
    d = datetime(year, month + 1, 1, 1, tzinfo=UTC) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _berlin_fallback(utc: datetime) -> datetime:
    """EU-Regel: Sommerzeit (UTC+2) von letztem Sonntag im März bis letztem Sonntag
    im Oktober, jeweils 01:00 UTC; sonst UTC+1."""
    start, end = _last_sunday_utc(utc.year, 3), _last_sunday_utc(utc.year, 10)
    off = 2 if start <= utc < end else 1
    return utc.astimezone(timezone(timedelta(hours=off)))


def berlin(now: datetime | None = None) -> datetime:
    """`now` (aware; naive gilt als UTC; None = jetzt) in Europe/Berlin."""
    utc = now or datetime.now(UTC)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=UTC)
    utc = utc.astimezone(UTC)
    try:
        from zoneinfo import ZoneInfo

        return utc.astimezone(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 - keine tzdata: Regel selbst rechnen
        return _berlin_fallback(utc)


def now_block(now: datetime | None = None) -> str:
    """Ein Satz: Wochentag, Datum, Uhrzeit in Berlin. Wissen, keine Regel."""
    b = berlin(now)
    return (f"Aktuelles Datum und Uhrzeit (Europe/Berlin): {_WEEKDAYS[b.weekday()]}, "
            f"{b.day}. {_MONTHS[b.month - 1]} {b.year}, {b.hour:02d}:{b.minute:02d} Uhr.")


Clock = Callable[[], datetime]


def system_now() -> datetime:
    return datetime.now(UTC)
