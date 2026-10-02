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
    # 230 -> 245 (02.10.2026, Aufg. 13/14): Warten statt Abbruch (--wait), Drain-Farbe-Check
    # vor dem Kopieren, Release-Staging; Box-Schritte liegen in infra/systemd/voicehook-release.
    assert _loc(ROOT / "deploy" / "deploy.sh") <= 245


def test_caddyfile_template_is_lean():
    # 30 -> 33 (01.10.2026): 3 Zeilen für die Footer-Seiten (PR #83), Platz für /login (PR #95).
    # 33 -> 34 (01.10.2026): SSE der HTTPS-Brücke nicht komprimieren (PR #99).
    # 34 -> 36 (02.10.2026): lb_try_duration überbrückt den HTTP-Neustart beim Deploy.
    # 36 -> 55 (02.10.2026): format filter schwärzt Geheimnisse im Access-Log (Sicherheitsbefund).
    # 55 -> 56 (02.10.2026): op_invite (CLI-Join) ebenfalls schwärzen.
    assert _loc(ROOT / "infra" / "caddy" / "Caddyfile.tmpl") <= 56


def test_systemd_units_are_http_plus_bluegreen_workers():
    # HTTP getrennt vom Worker (02.10.2026, Aufg. 14): zwei Worker-Farben laufen beim
    # Deploy gleichzeitig (alte drainet), HTTP gibt es genau einmal. Live-Worker analog.
    units = sorted(u.name for u in (ROOT / "infra" / "systemd").glob("*.service"))
    assert units == ["voicehook-agent-live@.service", "voicehook-agent@.service",
                     "voicehook-http.service"], units


def _unit(name):
    return (ROOT / "infra" / "systemd" / name).read_text()


def test_worker_units_run_start_and_drain():
    for name, drain in (("voicehook-agent@.service", 3600), ("voicehook-agent-live@.service", 1200)):
        u = _unit(name)
        assert "voicehook-run start" in u and " dev" not in u  # dev drainet nicht
        assert "Type=notify" in u and "NotifyAccess=main" in u
        assert "KillMode=mixed" in u and "KillSignal=SIGTERM" in u
        assert f"VH_DRAIN_TIMEOUT={drain}" in u
        stop = int(u.split("TimeoutStopSec=")[1].split()[0])
        assert stop > drain  # systemd darf den Drain nie vorzeitig per SIGKILL abbrechen
        assert "VOICEHOOK_HTTP_DISABLED=1" in u  # kein Port-Konflikt zwischen den Farben


def test_http_unit_is_http_only():
    u = _unit("voicehook-http.service")
    assert "voicehook-run http" in u and "Type=notify" not in u


def test_live_unit_has_no_http_and_own_agent_name():
    u = _unit("voicehook-agent-live@.service")
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


def test_caddy_access_log_redacts_secrets():
    """Access-Log schwärzt Wallet-Token, Auth/Cookies und Login/Invite/OAuth-Query (02.10.2026).

    Syntax Caddy 2.6.2 (Box): format filter { wrap console; fields { <feld> <filter> } }.
    """
    cf = (ROOT / "infra" / "caddy" / "Caddyfile.tmpl").read_text()
    assert "format filter {" in cf and "wrap console" in cf and "fields {" in cf
    for h in ("X-Wallet-Token", "Authorization", "Cookie"):
        assert f"request>headers>{h} replace REDACTED" in cf, h
    assert "resp_headers>Set-Cookie replace REDACTED" in cf
    assert "request>headers>Referer regexp" in cf and "resp_headers>Location regexp" in cf
    assert "request>uri query {" in cf
    for p in ("token", "nonce", "invite", "op_invite", "session_id", "r", "code", "state"):
        assert f"replace {p} REDACTED" in cf, p
    assert "format console" not in cf  # ungefiltertes Format wäre wieder Klartext


def test_caddy_access_log_not_world_readable():
    # Caddy 2.6.2 kennt `mode` unter `output file` nicht (wird still ignoriert), daher chmod im Deploy.
    d = (ROOT / "deploy" / "deploy.sh").read_text()
    assert "chmod 0640 /var/log/caddy/access.log" in d
