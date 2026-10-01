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
    # 80 -> 230 (30.09.2026): Web-only-Pfad, orb-ssh-agent und vor allem der
    # LiveKit-Preflight (kein Neustart, solange ein Mensch im Call ist) sind
    # Sicherheitsgewinne, keine Aufblähung. Weiter wachsen nur mit Begründung.
    assert _loc(ROOT / "deploy" / "deploy.sh") <= 230


def test_caddyfile_template_is_lean():
    # 30 -> 33 (01.10.2026): 3 Zeilen für die Footer-Seiten (PR #83), Platz für /login (PR #95).
    assert _loc(ROOT / "infra" / "caddy" / "Caddyfile.tmpl") <= 33


def test_systemd_units_are_exactly_main_plus_live_worker():
    # Hauptdienst (HTTP + Worker voice-ai) plus dedizierter Gemini-Live-Testworker
    # (Olli 30.09.2026: eigener Worker, damit der bestehende Ablauf nicht bricht).
    # Weitere Units bleiben verboten.
    units = sorted(u.name for u in (ROOT / "infra" / "systemd").glob("*.service"))
    assert units == ["voicehook-agent-live.service", "voicehook-agent.service"], units


def test_live_unit_has_no_http_and_own_agent_name():
    u = (ROOT / "infra" / "systemd" / "voicehook-agent-live.service").read_text()
    assert "VOICEHOOK_HTTP_DISABLED=1" in u          # kein zweiter Server auf :7400
    assert "VOICEHOOK_AGENT_NAME=voice-ai-live" in u  # nie als voice-ai dispatchbar
    assert "VOICEHOOK_PIPELINE=live" in u
    assert "VH_MAX_CALL_SECONDS=1200" in u            # engerer Kostendeckel für Live


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


def test_skill_is_under_200_loc():
    """Plan target: skill ≤200 LOC (v3 monster was 447)."""
    assert _loc(ROOT / "skills" / "voicehook-join" / "SKILL.md") <= 200


def test_footer_pages_exist_and_are_routed():
    """Footer-Links /impressum, /datenschutz, /setup, /security liefern echte Seiten (PR #83)."""
    for name in ("impressum", "datenschutz", "setup"):
        assert (ROOT / "web" / f"{name}.html").is_file(), name
    assert (ROOT / "web" / ".well-known" / "security.txt").is_file()
    cf = (ROOT / "infra" / "caddy" / "Caddyfile.tmpl").read_text()
    assert "@footer path /impressum /datenschutz /setup" in cf
    assert "rewrite @footer {path}.html" in cf
    assert "rewrite /security /.well-known/security.txt" in cf
    assert "try_files {path} /voice.html" in cf  # SPA-Fallback bleibt
