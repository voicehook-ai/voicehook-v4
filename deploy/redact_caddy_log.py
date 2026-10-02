#!/usr/bin/env python3
"""Schwärzt Geheimnisse in einem bestehenden Caddy-Access-Log (Altlast vor dem Log-Filter).

Gleiche Felder wie `format filter` in infra/caddy/Caddyfile.tmpl:
- Request-Header X-Wallet-Token, Authorization, Cookie und Response-Header Set-Cookie -> "REDACTED"
- Query-Parameter token, nonce, invite, session_id, r, code, state in request.uri, Referer und Location

Zeilenformat `format console`: Präfix (Zeit, Level, Logger, Nachricht, Tabs, ANSI-Farben) + JSON.
Das Präfix bleibt Byte für Byte erhalten, unveränderte Zeilen ebenso. Reines JSON (format json) geht auch.
Ausgabe in eine Temp-Datei im selben Verzeichnis, dann os.replace (atomar). Kein Klartext-Backup.
Danach `systemctl restart caddy`: Caddy schreibt sonst weiter in die alte, jetzt gelöschte Datei.

Aufruf: python3 deploy/redact_caddy_log.py /var/log/caddy/access.log
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from urllib.parse import unquote_plus

REDACTED = "REDACTED"
SECRET_HEADERS = {"x-wallet-token", "authorization", "cookie", "proxy-authorization"}
SECRET_RESP_HEADERS = {"set-cookie"}
SECRET_PARAMS = {"token", "nonce", "invite", "session_id", "r", "code", "state"}
_WALLET = re.compile(r"vhw_[A-Za-z0-9_\-]+")
_PARAM_RE = re.compile(r"([?&](?:" + "|".join(sorted(SECRET_PARAMS)) + r")=)[^&#\s\"]*")


def redact_url(url: str) -> str:
    """Ersetzt geheime Query-Werte; Reihenfolge, Pfad und Fragment bleiben unverändert."""
    if "?" not in url:
        return url
    head, _, rest = url.partition("?")
    query, hash_, frag = rest.partition("#")
    parts = []
    for pair in query.split("&"):
        key, eq, _val = pair.partition("=")
        if eq and unquote_plus(key) in SECRET_PARAMS:
            pair = f"{key}={REDACTED}"
        parts.append(pair)
    return f"{head}?{'&'.join(parts)}{hash_}{frag}"


def _redact_headers(headers: dict, secret: set[str]) -> None:
    for k in list(headers):
        if k.lower() in secret:
            headers[k] = REDACTED
        elif k.lower() in ("referer", "location"):
            v = headers[k]
            headers[k] = [redact_url(x) for x in v] if isinstance(v, list) else redact_url(str(v))


def redact_entry(d: dict) -> dict:
    req = d.get("request")
    if isinstance(req, dict):
        if isinstance(req.get("uri"), str):
            req["uri"] = redact_url(req["uri"])
        if isinstance(req.get("headers"), dict):
            _redact_headers(req["headers"], SECRET_HEADERS)
    if isinstance(d.get("resp_headers"), dict):
        _redact_headers(d["resp_headers"], SECRET_RESP_HEADERS)
    return d


def redact_line(line: str) -> str:
    body = line.rstrip("\n")
    nl = line[len(body):]
    i = body.find("{")
    if i < 0:
        return line
    prefix, raw = body[:i], body[i:]
    try:
        d = json.loads(raw)
    except ValueError:
        # Kaputtes JSON: grob per Regex, lieber zu viel als zu wenig schwärzen.
        out = _WALLET.sub("vhw_" + REDACTED, _PARAM_RE.sub(r"\1" + REDACTED, body))
        return out + nl
    if not isinstance(d, dict):
        return line
    new = redact_entry(json.loads(raw))
    if new == d and not _has_wallet(raw):
        return line  # nichts geschwärzt: Zeile exakt unverändert lassen
    return prefix + _WALLET.sub("vhw_" + REDACTED, json.dumps(new, ensure_ascii=False)) + nl


def _has_wallet(s: str) -> bool:
    return any(m.group(0) != "vhw_" + REDACTED for m in _WALLET.finditer(s))


def redact_file(path: str) -> int:
    """Schwärzt `path` atomar; gibt die Zahl geänderter Zeilen zurück."""
    st = os.stat(path)
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".redact-", dir=d)  # 0600, selbes Dateisystem
    changed = 0
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as src, \
                os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as dst:
            for line in src:
                out = redact_line(line)
                changed += out != line
                dst.write(out)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, st.st_mode & 0o7770)  # nie world-readable
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return changed


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    for p in argv:
        print(f"{p}: {redact_file(p)} Zeilen geschwärzt")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
