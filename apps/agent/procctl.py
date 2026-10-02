"""Worker-Prozesssteuerung für Blue/Green-Deploys ohne Call-Abbruch.

- `worker_option_kwargs()`: Produktions-Optionen des LiveKit-Workers aus der Env
  (`python -m agent start`; `dev` drainet beim Beenden NICHT, siehe docs/DEPLOY.md).
- `notify_ready()`: sd_notify READY=1, sobald der Worker bei LiveKit registriert ist.
  Die Units sind Type=notify, d. h. `systemctl start voicehook-agent@green` kehrt erst
  zurück, wenn der neue Worker Jobs annehmen kann; erst dann drainet deploy.sh den alten.
"""

from __future__ import annotations

import logging
import math
import os
import socket
from typing import Any

log = logging.getLogger("voicehook.procctl")


def _env_num(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        log.warning("%s=%r invalid, using %s", name, raw, default)
        return default
    if not math.isfinite(v) or not lo <= v <= hi:
        log.warning("%s=%r out of range [%s, %s], using %s", name, raw, lo, hi, default)
        return default
    return v


def worker_option_kwargs() -> dict[str, Any]:
    """WorkerOptions-Felder für den Produktionsmodus (nur in `start` wirksam).

    - load_threshold 0.7: über 70 % CPU-Last meldet sich der Worker voll (LiveKit-Default
      für start; dev hatte inf = nie voll).
    - num_idle_processes 1: ein vorgewärmter Job-Prozess (~300 MB) statt cpu_count (4 auf
      cx33). Während Blue/Green laufen zwei Farben x zwei Dienste parallel; 8 GB RAM.
    - drain_timeout: so lange darf der alte Worker nach SIGTERM laufende Calls zu Ende
      führen (Normal 3600 s = CallGuard-Deckel 60 min, Live 1200 s = VH_MAX_CALL_SECONDS).
    - port: Health-Server des Workers; 0 = freier Port (Prod-Default 8081 würde zwischen
      blue und green und dem Live-Dienst kollidieren).
    """
    return {
        "load_threshold": _env_num("VH_WORKER_LOAD_THRESHOLD", 0.7, lo=0.05, hi=0.99),
        "num_idle_processes": int(_env_num("VH_WORKER_IDLE_PROCS", 1, lo=0, hi=8)),
        "drain_timeout": int(_env_num("VH_DRAIN_TIMEOUT", 3600, lo=0, hi=6 * 3600)),
        "port": int(_env_num("VH_WORKER_HTTP_PORT", 0, lo=0, hi=65535)),
    }


def notify_ready(*_args: Any) -> bool:
    """sd_notify(READY=1); no-op ohne NOTIFY_SOCKET (lokal, Tests). Nie werfen."""
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(b"READY=1\nSTATUS=worker registered")
        log.info("sd_notify READY=1 (worker registered)")
        return True
    except OSError as e:
        log.warning("sd_notify failed: %s", e)
        return False
