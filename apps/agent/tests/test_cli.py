"""CLI tests — invite-mint subcommand returns a valid join URL."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from agent.cli import main
from agent.tokens import verify_invite


def test_invite_prints_join_url(capsys, monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", "test-secret")
    rc = main(["invite", "demo-room-live-ABC5"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    u = urlparse(out)
    assert u.scheme == "https"
    assert u.netloc == "voicehook.ai"
    assert u.path == "/r/demo-room-live-ABC5"
    code = parse_qs(u.query)["invite"][0]
    v = verify_invite(code, "demo-room-live-ABC5", secret="test-secret")
    assert v.valid


def test_invite_custom_base(capsys, monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", "x")
    rc = main(["invite", "drift-signal-crisp-PDM5", "--base", "https://staging.voicehook.ai", "--ttl", "300"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("https://staging.voicehook.ai/r/drift-signal-crisp-PDM5?")


def test_invite_fails_without_secret(monkeypatch):
    monkeypatch.delenv("INVITE_SECRET", raising=False)
    rc = main(["invite", "x"])
    assert rc == 2


def test_unknown_subcommand_errors():
    with pytest.raises(SystemExit):
        main(["nope"])
