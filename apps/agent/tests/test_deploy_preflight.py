"""Paket 7, Punkt 9: deploy.sh-Preflight zählt CLI-/Operator-Agenten (vh.role=agent,
z. B. claude-crown-69e3) nicht als Menschen."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[3] / "deploy" / "deploy.sh"


def _agentish():
    src = DEPLOY.read_text()
    m = re.search(r"^def agentish\(p\):.*?(?=^\S)", src, re.S | re.M)
    assert m, "agentish() not found in deploy.sh preflight"
    ns: dict = {}
    exec(m.group(0), ns)  # noqa: S102  (eigener Repo-Code)
    return ns["agentish"]


@pytest.mark.parametrize("p", [
    {"identity": "voice-ai-AJ_x", "kind": "AGENT"},
    {"identity": "agent-123", "kind": 4},
    {"identity": "claude-crown-69e3", "kind": "STANDARD", "attributes": {"vh.role": "agent", "vh.name": "Claude"}},
    {"identity": "hermes-vm-9f", "attributes": {"vh.role": "agent"}},
])
def test_agents_are_not_humans(p):
    assert _agentish()(p)


@pytest.mark.parametrize("p", [
    {"identity": "host-user", "kind": "STANDARD"},
    {"identity": "guest-ab12", "kind": 0, "attributes": {}},
    {"identity": "x", "attributes": {"vh.role": "human"}},
    {"identity": "y", "attributes": None},
])
def test_humans_stay_humans(p):
    assert not _agentish()(p)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_deploy_sh_shellcheck():
    r = subprocess.run(["shellcheck", "-S", "error", str(DEPLOY)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_deploy_sh_syntax():
    r = subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
