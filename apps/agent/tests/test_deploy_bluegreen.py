"""Aufgaben 13/14: Preflight vor dem Kopieren, Release-Symlink, Blue/Green mit Drain.

deploy.sh läuft hier gegen Stubs (DEPLOY_SSH / DEPLOY_RSYNC protokollieren nur), die
Box-Seite (infra/systemd/voicehook-release) gegen ein echtes Temp-Dateisystem mit
systemctl-Stub. Der echte Drain gegen einen LiveKit-Server: tests/e2e/drain_bluegreen.py.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import pytest

from agent import procctl

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy" / "deploy.sh"
RELEASE = ROOT / "infra" / "systemd" / "voicehook-release"
RUN = ROOT / "infra" / "systemd" / "voicehook-run"


# ---------------------------------------------------------------- deploy.sh (stubbed box)
FAKE_SSH = """#!/usr/bin/env bash
# argv: <host> <command...>; stdin may carry the preflight python
shift; cmd="$*"
printf 'SSH %s\\n' "$cmd" >> "$LOG"
case "$cmd" in
  *"timeout 90 python3 -"*) cat >/dev/null; echo "PREFLIGHT rc=$PRE_RC" >> "$LOG"; exit "$PRE_RC" ;;
  *"is-active voicehook-agent voicehook-agent@blue"*) echo active; echo inactive; echo inactive ;;
  *"is-active voicehook-agent@"*) printf '%s\\n' $BUSY ;;
  *".color"*) echo green ;;  # box: .color=blue -> next color green
esac
exit 0
"""
FAKE_RSYNC = """#!/usr/bin/env bash
printf 'RSYNC %s\\n' "$*" >> "$LOG"
"""


def _deploy(tmp_path: Path, *, pre_rc: int, busy: str = "inactive inactive", args=("--full",)):
    log = tmp_path / "log"
    for name, body in (("ssh", FAKE_SSH), ("rsync", FAKE_RSYNC)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(0o755)
    env = {**os.environ, "LOG": str(log), "PRE_RC": str(pre_rc), "BUSY": busy,
           "BOX_HOST": "root@box.test", "DEPLOY_SSH": str(tmp_path / "ssh"),
           "DEPLOY_RSYNC": str(tmp_path / "rsync")}
    r = subprocess.run(["bash", str(DEPLOY), *args], env=env, capture_output=True, text=True,
                       timeout=60)
    return r, (log.read_text().splitlines() if log.exists() else [])


def _copies(lines):
    return [ln for ln in lines if ln.startswith("RSYNC") or "mkdir" in ln or "voicehook-release" in ln]


def test_human_in_call_nothing_is_copied(tmp_path):
    r, lines = _deploy(tmp_path, pre_rc=3)
    assert r.returncode == 1
    assert "NOTHING was copied" in r.stderr
    assert "PREFLIGHT rc=3" in lines
    assert _copies(lines) == [], lines  # kein rsync, kein mkdir, keine Aktivierung


def test_preflight_error_fails_closed_before_copy(tmp_path):
    r, lines = _deploy(tmp_path, pre_rc=1, args=("--full", "--force-restart"))
    assert r.returncode == 1 and _copies(lines) == []


def test_draining_color_blocks_before_copy(tmp_path):
    # Vorheriger Deploy drainet noch auf green: nichts kopieren, klare Meldung.
    r, lines = _deploy(tmp_path, pre_rc=0, busy="deactivating inactive")
    assert r.returncode == 1
    assert "green is still deactivating" in r.stderr and "NOTHING copied" in r.stderr
    assert _copies(lines) == []


def test_order_preflight_stage_then_atomic_activate(tmp_path):
    r, lines = _deploy(tmp_path, pre_rc=0)
    assert r.returncode == 0, r.stderr
    idx = {k: next(i for i, ln in enumerate(lines) if k in ln) for k in (
        "PREFLIGHT rc=0", "RSYNC", "voicehook-release venv", "Caddyfile",
        "voicehook-release activate", "> /var/www/voicehook/.deployed-sha")}
    assert idx["PREFLIGHT rc=0"] < idx["RSYNC"] < idx["voicehook-release venv"]
    assert idx["voicehook-release venv"] < idx["voicehook-release activate"] < idx["> /var/www/voicehook/.deployed-sha"]
    # Code landet nur im neuen Release-Verzeichnis, nie im laufenden Baum
    code = [ln for ln in lines if ln.startswith("RSYNC") and "apps/agent/" in ln]
    assert code and all("/opt/voicehook/releases/" in ln for ln in code), code
    assert not any("/opt/voicehook/apps/agent" in ln for ln in lines)
    assert not any("systemctl restart voicehook-agent" in ln for ln in lines)


def test_web_only_skips_preflight(tmp_path):
    r, lines = _deploy(tmp_path, pre_rc=3, args=("--web-only",))
    assert r.returncode == 0, r.stderr
    assert not any("PREFLIGHT" in ln for ln in lines)


# ---------------------------------------------------------------- box side (real fs, stub systemctl)
def _box(tmp_path: Path, *, fail_start: bool = False):
    root, units = tmp_path / "opt", tmp_path / "units"
    (root / "releases").mkdir(parents=True)
    units.mkdir()
    sc = tmp_path / "systemctl"
    sc.write_text('#!/usr/bin/env bash\nprintf "SC %s\\n" "$*" >> "$LOG"\n'
                  + ('[ "$1" = start ] && exit 1\n' if fail_start else "") + "exit 0\n")
    sc.chmod(0o755)
    env = {**os.environ, "VOICEHOOK_ROOT": str(root), "VOICEHOOK_DOCROOT": str(tmp_path / "www"),
           "VOICEHOOK_UNITDIR": str(units), "VOICEHOOK_SYSTEMCTL": str(sc),
           "LOG": str(tmp_path / "sclog")}
    (tmp_path / "www").mkdir()
    return root, units, env


def _release(root: Path, name: str) -> Path:
    rel = root / "releases" / name
    (rel / "apps" / "agent").mkdir(parents=True)
    (rel / "web").mkdir()
    (rel / "web" / "v.txt").write_text(name)
    return rel


def _activate(rel, env):
    return subprocess.run(["bash", str(RELEASE), "activate", str(rel)], env=env,
                          capture_output=True, text=True, timeout=30)


def _sc(tmp_path):
    return (tmp_path / "sclog").read_text().splitlines()


def test_activate_blue_green_swap_and_drain_order(tmp_path):
    root, units, env = _box(tmp_path)
    (units / "voicehook-agent.service").write_text("legacy")
    r1, r2 = _release(root, "r1"), _release(root, "r2")

    assert _activate(r1, env).returncode == 0
    assert os.readlink(root / "current") == str(r1)
    assert (root / ".color").read_text().strip() == "blue"
    first = _sc(tmp_path)
    # Migration: neue Farbe läuft (registriert), BEVOR der alte dev-Dienst gestoppt wird
    i_start = first.index("SC start voicehook-agent@blue voicehook-agent-live@blue")
    i_legacy = first.index("SC disable --now voicehook-agent.service")
    assert i_start < i_legacy < first.index("SC restart voicehook-http.service")
    assert not (units / "voicehook-agent.service").exists()
    assert not any("stop" in ln for ln in first)  # keine alte Farbe beim ersten Mal

    (tmp_path / "sclog").unlink()
    assert _activate(r2, env).returncode == 0
    assert os.readlink(root / "current") == str(r2)
    assert (root / ".color").read_text().strip() == "green"
    second = _sc(tmp_path)
    i_start = second.index("SC start voicehook-agent@green voicehook-agent-live@green")
    i_stop = second.index("SC stop --no-block voicehook-agent@blue voicehook-agent-live@blue")
    assert i_start < second.index("SC restart voicehook-http.service") < i_stop
    assert (tmp_path / "www" / "v.txt").read_text() == "r2"


def test_activate_rolls_back_when_new_color_does_not_register(tmp_path):
    root, _units, env = _box(tmp_path)
    r1, r2 = _release(root, "r1"), _release(root, "r2")
    assert _activate(r1, env).returncode == 0
    env["VOICEHOOK_SYSTEMCTL"] = str(_box(tmp_path / "x", fail_start=True)[2]["VOICEHOOK_SYSTEMCTL"])
    env["LOG"] = str(tmp_path / "x" / "sclog")
    r = _activate(r2, env)
    assert r.returncode == 1 and "rollback" in r.stderr
    assert os.readlink(root / "current") == str(r1)          # Symlink zurück
    assert (root / ".color").read_text().strip() == "blue"    # alte Farbe bleibt aktiv
    log = (tmp_path / "x" / "sclog").read_text()
    assert "restart voicehook-http" not in log and "stop --no-block" not in log


def test_prune_keeps_current_and_newest(tmp_path):
    root, _units, env = _box(tmp_path)
    rels = []
    for i in range(8):
        rels.append(_release(root, f"r{i}"))
        os.utime(rels[-1], (1_000_000 + i, 1_000_000 + i))
    assert _activate(rels[-1], env).returncode == 0
    left = sorted(p.name for p in (root / "releases").iterdir())
    assert left == ["r3", "r4", "r5", "r6", "r7"]


def test_launcher_pins_real_release_path(tmp_path):
    """voicehook-run löst current EINMAL auf: cwd/sys.path zeigen auf das echte Release."""
    root = tmp_path / "opt"
    rel = root / "releases" / "r1"
    pkg = rel / "apps" / "agent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "__main__.py").write_text("import os, sys; print(os.getcwd()); print(sys.path[0])\n")
    venv = root / "venvs" / "h1" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(os.path.realpath(os.sys.executable))
    (rel / "venv").symlink_to("../../venvs/h1")
    (root / "current").symlink_to(rel)
    out = subprocess.run(["sh", str(RUN)], env={**os.environ, "VOICEHOOK_ROOT": str(root)},
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    cwd, p0 = out.stdout.split()
    assert cwd == str(pkg.parent.resolve()) and p0 == cwd and "current" not in cwd


# ---------------------------------------------------------------- procctl
def test_worker_options_prod_defaults(monkeypatch):
    for k in ("VH_WORKER_LOAD_THRESHOLD", "VH_WORKER_IDLE_PROCS", "VH_DRAIN_TIMEOUT",
              "VH_WORKER_HTTP_PORT"):
        monkeypatch.delenv(k, raising=False)
    assert procctl.worker_option_kwargs() == {
        "load_threshold": 0.7, "num_idle_processes": 1, "drain_timeout": 3600, "port": 0}


@pytest.mark.parametrize("val", ["inf", "nan", "-1", "abc", "1.5"])
def test_worker_options_bad_threshold_falls_back(monkeypatch, val):
    monkeypatch.setenv("VH_WORKER_LOAD_THRESHOLD", val)
    assert procctl.worker_option_kwargs()["load_threshold"] == 0.7


def test_worker_options_drain_from_env(monkeypatch):
    monkeypatch.setenv("VH_DRAIN_TIMEOUT", "1200")
    monkeypatch.setenv("VH_WORKER_IDLE_PROCS", "2")
    kw = procctl.worker_option_kwargs()
    assert kw["drain_timeout"] == 1200 and kw["num_idle_processes"] == 2


def test_build_worker_options_carries_drain(monkeypatch):
    monkeypatch.setenv("VH_DRAIN_TIMEOUT", "1200")
    from agent.worker import build_worker_options
    o = build_worker_options()
    assert o.drain_timeout == 1200 and o.load_threshold == 0.7 and o.num_idle_processes == 1


def test_notify_ready_sends_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
        srv.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert procctl.notify_ready("worker-id", None) is True
        assert srv.recv(256).startswith(b"READY=1")


def test_notify_ready_noop_without_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert procctl.notify_ready() is False
