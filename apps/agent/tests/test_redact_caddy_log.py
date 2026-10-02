"""deploy/redact_caddy_log.py schwärzt Altlast im Caddy-Access-Log (02.10.2026)."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("redact_caddy_log", ROOT / "deploy" / "redact_caddy_log.py")
rcl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rcl)

PREFIX = "2026/10/02 14:04:29.398\t\x1b[34mINFO\x1b[0m\thttp.log.access.log0\thandled request\t"
SECRETS = ("vhw_TEST", "SECRET", "NONCEX", "INVITEX", "AUTHX", "COOKIEX", "SETX", "RCODE", "OAUTHX", "STATEX", "SESSX")


def _entry():
    return {
        "request": {
            "remote_ip": "1.2.3.4", "method": "GET", "host": "voicehook.ai",
            "uri": "/api/login/verify?token=SECRET&nonce=NONCEX&keep=1&r=RCODE",
            "headers": {
                "User-Agent": ["PosKontrolle/1.0"], "X-Wallet-Token": ["vhw_TEST"],
                "Authorization": ["Bearer AUTHX"], "Cookie": ["vh=COOKIEX"],
                "Referer": ["https://voicehook.ai/r/room1?invite=INVITEX#r=frag"],
            },
        },
        "status": 302,
        "resp_headers": {"Set-Cookie": ["vh=SETX"], "Location": ["/aufladen?session_id=SESSX"]},
    }


def _line(d):
    return PREFIX + json.dumps(d) + "\n"


def test_redacts_all_secret_fields_and_keeps_prefix():
    out = rcl.redact_line(_line(_entry()))
    assert out.startswith(PREFIX) and out.endswith("\n")
    for s in SECRETS:
        assert s not in out, s
    d = json.loads(out[len(PREFIX):])
    r = d["request"]
    assert r["uri"] == "/api/login/verify?token=REDACTED&nonce=REDACTED&keep=1&r=REDACTED"
    assert r["headers"]["User-Agent"] == ["PosKontrolle/1.0"]  # bleibt
    assert r["headers"]["X-Wallet-Token"] == "REDACTED"
    assert r["headers"]["Referer"] == ["https://voicehook.ai/r/room1?invite=REDACTED#r=frag"]
    assert d["status"] == 302 and d["resp_headers"]["Set-Cookie"] == "REDACTED"
    assert d["resp_headers"]["Location"] == ["/aufladen?session_id=REDACTED"]


def test_oauth_params_and_encoded_keys():
    assert rcl.redact_url("/api/oauth/cb?code=OAUTHX&state=STATEX&scope=a") == \
        "/api/oauth/cb?code=REDACTED&state=REDACTED&scope=a"
    assert rcl.redact_url("/x?%74oken=SECRET") == "/x?%74oken=REDACTED"
    assert rcl.redact_url("/x?tokenx=keep&rr=keep") == "/x?tokenx=keep&rr=keep"


def test_clean_line_is_byte_identical_and_idempotent():
    clean = PREFIX + '{"request": {"uri": "/healthz", "headers": {}}, "duration": 1e-05}\n'
    assert rcl.redact_line(clean) == clean
    once = rcl.redact_line(_line(_entry()))
    assert rcl.redact_line(once) == once


def test_broken_json_falls_back_to_regex():
    out = rcl.redact_line(PREFIX + '{"request": {"uri": "/a?token=SECRET", "x": "vhw_TEST"\n')
    assert "SECRET" not in out and "vhw_TEST" not in out and out.startswith(PREFIX)


def test_redact_file_is_atomic_without_backup(tmp_path):
    log = tmp_path / "access.log"
    clean = PREFIX + '{"request": {"uri": "/"}}\n'
    log.write_text(clean + _line(_entry()) + "not json at all\n")
    os.chmod(log, 0o644)
    assert rcl.redact_file(str(log)) == 1
    text = log.read_text()
    assert text.startswith(clean) and text.endswith("not json at all\n")
    for s in SECRETS:
        assert s not in text
    assert [p.name for p in tmp_path.iterdir()] == ["access.log"]  # kein Backup, kein Temp-Rest
    assert os.stat(log).st_mode & 0o777 == 0o640  # nie world-readable
