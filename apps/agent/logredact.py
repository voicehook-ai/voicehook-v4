"""Access-Log ohne Geheimnisse (Datenschutz, Oliver 02.10.).

uvicorn.access schreibt die volle Anfrage-URL. Beim OAuth-Callback, bei
/api/login/verify und bei Einladungslinks stehen darin Code, State, Token, Nonce
und Einladungen. Der Filter ersetzt nur deren Werte durch "…"; Pfad, übrige
Parameter, Status und Client bleiben lesbar."""

from __future__ import annotations

import logging
import re

SECRET_PARAMS = ("code", "state", "token", "nonce", "invite", "op_invite")
REDACTED = "…"
_RE = re.compile(r"([?&](?:" + "|".join(SECRET_PARAMS) + r")=)[^&#\s\"]*", re.IGNORECASE)


def redact_query(text: str) -> str:
    """Werte der Geheim-Parameter in einer URL/Logzeile durch "…" ersetzen."""
    return _RE.sub(lambda m: m.group(1) + REDACTED, text) if isinstance(text, str) else text


class AccessLogRedactor(logging.Filter):
    """uvicorn.access: args = (client, method, full_path, http_version, status)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            record.args = tuple(redact_query(a) if isinstance(a, str) else a for a in args)
        elif isinstance(args, dict):
            record.args = {k: redact_query(v) if isinstance(v, str) else v for k, v in args.items()}
        if isinstance(record.msg, str):
            record.msg = redact_query(record.msg)
        return True


def install(logger_name: str = "uvicorn.access") -> AccessLogRedactor:
    """Filter einmal an den Logger hängen (idempotent)."""
    lg = logging.getLogger(logger_name)
    for f in lg.filters:
        if isinstance(f, AccessLogRedactor):
            return f
    f = AccessLogRedactor()
    lg.addFilter(f)
    return f
