"""Mailversand für den Magic-Link-Login über die Resend-API (ohne SDK).

POST https://api.resend.com/emails, Bearer <Key>, JSON {from, to, subject,
html, text} (https://resend.com/docs/api-reference/emails/send-email).

Env: RESEND_SENDING_API_KEY = Resend-Key mit Recht "Sending access" (nur Senden).
     Auf den Server gehört NUR dieser Key, nie der Full-Access-Key fürs Domain-Management.
     RESEND_API_KEY wird als Fallback weiter gelesen (bestehende .env), sollte aber
     ebenfalls nur ein Sending-Key sein. Ohne Key ist der Login aus (/api/login -> 503).
     MAIL_FROM (Default "voicehook <login@voicehook.ai>", Domain in Resend verifiziert).
Die Empfängeradresse landet nie im Log und nie in einer MailError-Meldung.
"""

from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.request

RESEND_API = "https://api.resend.com/emails"
DEFAULT_FROM = "voicehook <login@voicehook.ai>"
# Resend steht hinter Cloudflare, das den urllib-Default-UA mit 403 "error code: 1010"
# abweist (gemessen 02.10.). Ohne eigenen User-Agent geht keine Mail raus.
USER_AGENT = "voicehook-agent/1.0 (+https://voicehook.ai)"


class MailError(RuntimeError):
    pass


KEY_ENVS = ("RESEND_SENDING_API_KEY", "RESEND_API_KEY")


def api_key() -> str:
    for name in KEY_ENVS:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return ""


def configured() -> bool:
    return bool(api_key())


def sender() -> str:
    return os.environ.get("MAIL_FROM", "").strip() or DEFAULT_FROM


def login_message(link: str, minutes: int) -> dict:
    """Betreff + Text der Login-Mail (deutsch, kurz, ohne Tracking)."""
    text = (
        "Hallo,\n\n"
        f"mit diesem Link meldest du dich bei voicehook an (gültig {minutes} Minuten, einmal):\n\n"
        f"{link}\n\n"
        "Wenn du das nicht angefordert hast, ignoriere diese Mail. Ohne Klick passiert nichts.\n\n"
        "voicehook.ai\n"
    )
    safe = html.escape(link, quote=True)
    body = (
        "<p>Hallo,</p>"
        f"<p>mit diesem Link meldest du dich bei voicehook an (gültig {minutes} Minuten, einmal):</p>"
        f'<p><a href="{safe}">Bei voicehook anmelden</a></p>'
        "<p>Wenn du das nicht angefordert hast, ignoriere diese Mail. Ohne Klick passiert nichts.</p>"
        "<p>voicehook.ai</p>"
    )
    return {"subject": "Dein Login für voicehook", "text": text, "html": body}


def send(to: str, message: dict, *, timeout: float = 10.0) -> None:
    """Mail verschicken; MailError bei fehlendem Key oder Fehler des Anbieters."""
    key = api_key()
    if not key:
        raise MailError("RESEND_SENDING_API_KEY not configured")
    payload = {"from": sender(), "to": [to], **message}
    req = urllib.request.Request(
        RESEND_API,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise MailError(f"resend {e.code}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise MailError(f"resend unreachable: {e}") from e
