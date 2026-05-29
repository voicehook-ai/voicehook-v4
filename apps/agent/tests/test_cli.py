"""CLI tests — invite-mint subcommand returns a valid join URL."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from agent.cli import main
from agent.tokens import verify_invite


def test_invite_prints_join_url(capsys, monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", "test-secret")
    rc = main(["invite", "demo-room-abc"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    u = urlparse(out)
    assert u.scheme == "https"
    assert u.netloc == "voicehook.ai"
    assert u.path == "/r/demo-room-abc"
    code = parse_qs(u.query)["invite"][0]
    v = verify_invite(code, "demo-room-abc", secret="test-secret")
    assert v.valid


def test_invite_custom_base(capsys, monkeypatch):
    monkeypatch.setenv("INVITE_SECRET", "x")
    rc = main(["invite", "room-y", "--base", "https://staging.voicehook.ai", "--ttl", "300"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("https://staging.voicehook.ai/r/room-y?")


def test_invite_fails_without_secret(monkeypatch):
    monkeypatch.delenv("INVITE_SECRET", raising=False)
    rc = main(["invite", "x"])
    assert rc == 2


def test_unknown_subcommand_errors():
    with pytest.raises(SystemExit):
        main(["nope"])
