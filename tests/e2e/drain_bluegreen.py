"""Blue/Green-Drain gegen einen echten lokalen LiveKit-Server (nie Prod).

Belegt Aufgabe 13/14 mit echten Prozessen, echter Registrierung und echten Jobs:
  1. laufender Job auf blue überlebt Symlink-Swap + Start green + SIGTERM an blue
  2. neuer Job nach dem Swap geht an green (neuer Code)
  3. keine Mischversion: ein Job, den blue NACH dem Swap noch annimmt, läuft mit altem Code
  4. blue beendet sich selbst, sobald seine Jobs fertig sind (Exit 0, vor drain_timeout)
Der Worker ist ein Sim-Worker (Entrypoint schreibt Herzschläge mit Release-Version),
gestartet über den echten Launcher infra/systemd/voicehook-run und agent/procctl.py
(Prod-Optionen + sd_notify READY). Was systemd tut, macht das Skript von Hand:
Type=notify -> auf READY warten, KillMode=mixed -> SIGTERM nur an den Hauptprozess.

  LIVEKIT_SERVER=/pfad/livekit-server .venv/bin/python tests/e2e/drain_bluegreen.py
  ... drain_bluegreen.py dev   # Gegenprobe: alter dev-Modus, Call stirbt bei SIGTERM -> FAIL erwartet
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from livekit import api

REPO = Path(__file__).resolve().parents[2]
URL, KEY, SECRET, PORT = "ws://127.0.0.1:7990", "devkey", "secret", 7990
JOB_SECONDS = 25
MODE = sys.argv[1] if len(sys.argv) > 1 else "start"

SIM = '''
import asyncio, os, time
from livekit.agents import AgentServer, JobContext, WorkerOptions, cli
from agent import procctl
from agent.version import VERSION

async def entrypoint(ctx: JobContext):
    await ctx.connect()
    end = time.time() + float(os.environ["SIM_JOB_SECONDS"])
    while time.time() < end:
        with open(os.path.join(os.environ["SIM_OUT"], ctx.room.name + ".log"), "a") as f:
            f.write(f"{time.time():.1f} {VERSION} {os.environ['COLOR']} {os.getpid()}\\n")
        await asyncio.sleep(1)
    ctx.shutdown("sim done")

MAIN = """
from livekit.agents import AgentServer, WorkerOptions, cli
from agent import procctl
from agent.sim import entrypoint  # Entrypoint in einem Modul (forkserver pickelt per Name)

if __name__ == "__main__":
    s = AgentServer.from_server_options(WorkerOptions(
        entrypoint_fnc=entrypoint, agent_name="sim", **procctl.worker_option_kwargs()))
    s.on("worker_registered", procctl.notify_ready)
    cli.run_app(s)
"""
'''


def release(root: Path, name: str, version: str) -> Path:
    pkg = root / "releases" / name / "apps" / "agent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    sim, main = SIM.split('MAIN = """')
    (pkg / "sim.py").write_text(sim)
    (pkg / "__main__.py").write_text(main.rsplit('"""', 1)[0])
    (pkg / "version.py").write_text(f"VERSION = {version!r}\n")
    shutil.copy(REPO / "apps" / "agent" / "procctl.py", pkg / "procctl.py")
    (root / "releases" / name / "venv").symlink_to("../../venvs/shared")
    return pkg.parent.parent


def swap(root: Path, rel: Path) -> None:
    (root / "current.new").symlink_to(rel)
    os.replace(root / "current.new", root / "current")  # = mv -T, atomar


def start_color(root: Path, out: Path, color: str) -> tuple[subprocess.Popen, float]:
    sock_path = str(out / f"{color}.notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(sock_path)
    env = {**os.environ, "VOICEHOOK_ROOT": str(root), "LIVEKIT_URL": URL, "LIVEKIT_API_KEY": KEY,
           "LIVEKIT_API_SECRET": SECRET, "NOTIFY_SOCKET": sock_path, "COLOR": color,
           "SIM_OUT": str(out), "SIM_JOB_SECONDS": str(JOB_SECONDS), "VH_DRAIN_TIMEOUT": "120"}
    log = open(out / f"{color}.stdout", "w")  # noqa: SIM115
    p = subprocess.Popen(["sh", str(REPO / "infra/systemd/voicehook-run"), MODE], env=env,
                         stdout=log, stderr=subprocess.STDOUT)
    t0 = time.time()
    srv.settimeout(60)
    msg = srv.recv(256)  # = systemd Type=notify: start kehrt erst nach READY zurück
    assert msg.startswith(b"READY=1"), msg
    return p, time.time() - t0


async def dispatch(room: str) -> None:
    async with api.LiveKitAPI(URL.replace("ws", "http"), KEY, SECRET) as lk:
        await lk.room.create_room(api.CreateRoomRequest(name=room, empty_timeout=300))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(agent_name="sim", room=room))


def beats(out: Path, room: str) -> list[list[str]]:
    f = out / f"{room}.log"
    return [ln.split() for ln in f.read_text().splitlines()] if f.exists() else []


def wait_beats(out: Path, room: str, timeout: float = 30) -> list[list[str]]:
    end = time.time() + timeout
    while time.time() < end:
        if b := beats(out, room):
            return b
        time.sleep(0.3)
    raise AssertionError(f"no job in {room}")


def main() -> int:
    lk_bin = os.environ.get("LIVEKIT_SERVER", "livekit-server")
    out = Path(tempfile.mkdtemp(prefix="vh-drain-"))
    root = out / "opt"
    (root / "venvs").mkdir(parents=True)
    (root / "venvs" / "shared").symlink_to(Path(sys.prefix))
    r1, r2 = release(root, "r1", "v1"), release(root, "r2", "v2")
    swap(root, r1)
    lk = subprocess.Popen([lk_bin, "--dev", "--bind", "127.0.0.1", "--port", str(PORT)],
                          stdout=open(out / "lk.log", "w"), stderr=subprocess.STDOUT)  # noqa: SIM115
    ev: dict = {"tmp": str(out), "mode": MODE}
    blue = green = None
    try:
        time.sleep(2)
        blue, ev["blue_ready_s"] = start_color(root, out, "blue")
        asyncio.run(dispatch("call-a"))                      # laufender Call auf blue
        a0 = wait_beats(out, "call-a")
        swap(root, r2)                                       # Release-Swap (deploy: activate)
        asyncio.run(dispatch("call-a2"))                     # blue nimmt nach Swap noch einen an
        a2 = wait_beats(out, "call-a2")
        green, ev["green_ready_s"] = start_color(root, out, "green")
        t_term = time.time()
        blue.send_signal(signal.SIGTERM)                     # systemctl stop (KillMode=mixed)
        time.sleep(1)
        asyncio.run(dispatch("call-b"))                      # neuer Call nach dem Deploy
        b = wait_beats(out, "call-b")
        blue_rc = blue.wait(timeout=150)
        t_exit = time.time()
        a_all, a2_all = beats(out, "call-a"), beats(out, "call-a2")
        ev.update({
            "call_a": {"version": sorted({x[1] for x in a_all}), "color": sorted({x[2] for x in a_all}),
                       "beats": len(a_all), "beats_after_sigterm": sum(float(x[0]) > t_term for x in a_all),
                       "last_beat_after_sigterm_s": round(float(a_all[-1][0]) - t_term, 1)},
            "call_a2_started_after_swap_on_blue": {"version": sorted({x[1] for x in a2_all}),
                                                   "color": sorted({x[2] for x in a2_all})},
            "call_b_after_deploy": {"version": b[0][1], "color": b[0][2]},
            "blue_exit_code": blue_rc,
            "blue_exit_after_sigterm_s": round(t_exit - t_term, 1),
            "blue_drain_log": [ln.strip()[:160] for ln in (out / "blue.stdout").read_text().splitlines()
                               if "drain" in ln.lower()][:3],
        })
        ok = (ev["call_a"]["version"] == ["v1"] and ev["call_a"]["color"] == ["blue"]
              and ev["call_a"]["beats"] >= JOB_SECONDS - 1 and a0 and a2  # Call lief bis zum Ende
              and ev["call_a2_started_after_swap_on_blue"]["version"] == ["v1"]
              and ev["call_b_after_deploy"] == {"version": "v2", "color": "green"}
              and blue_rc == 0 and ev["blue_exit_after_sigterm_s"] < 120)
        ev["PASS"] = bool(ok)
        print(json.dumps(ev, indent=2))
        return 0 if ok else 1
    finally:
        for p in (blue, green):
            if p and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    p.kill()
        lk.terminate()


if __name__ == "__main__":
    sys.exit(main())
