"""Infra-files smoke: line-count caps + presence of required pieces.

Plan targets (PLAN-v4.md §Success criteria):
- deploy.sh ≤ 80 LOC
- Caddyfile ≤ 30 LOC
- Skill ≤ 200 LOC (PR-9)
- agent.py ≤ 500 LOC (PR-10 budget check)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _loc(p: Path) -> int:
    return sum(1 for _ in p.read_text().splitlines())


def test_deploy_script_is_lean():
    assert _loc(ROOT / "deploy" / "deploy.sh") <= 80


def test_caddyfile_template_is_lean():
    assert _loc(ROOT / "infra" / "caddy" / "Caddyfile.tmpl") <= 30


def test_single_systemd_unit():
    units = list((ROOT / "infra" / "systemd").glob("*.service"))
    assert len(units) == 1, f"expected 1 systemd unit (voicehook-agent), found {units}"
    assert units[0].name == "voicehook-agent.service"


def test_terraform_files_present():
    tf_dir = ROOT / "infra" / "terraform"
    for f in ("main.tf", "variables.tf", "versions.tf", "cloud-init.yaml"):
        assert (tf_dir / f).is_file(), f"missing {f}"


def test_voice_html_serves_token_then_joins():
    """Sanity: web/voice.html mints via /api/token + uses LiveKit JS SDK."""
    html = (ROOT / "web" / "voice.html").read_text()
    assert "/api/token" in html
    assert "livekit-client" in html
    assert "setMicrophoneEnabled" in html


def test_caddyfile_only_routes_to_agent_port():
    """No legacy v3 :7400 cruft — only ONE reverse_proxy."""
    cf = (ROOT / "infra" / "caddy" / "Caddyfile.tmpl").read_text()
    assert cf.count("reverse_proxy 127.0.0.1:7400") == 1
    # plus the LK websocket
    assert cf.count("reverse_proxy 127.0.0.1:7880") == 1
