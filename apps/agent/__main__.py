"""Entrypoint: FastAPI surface and/or LiveKit worker.

- `python -m agent http`: nur HTTP (:7400), eigener Dienst `voicehook-http` auf der Box.
  uvicorn läuft im Hauptthread und beendet sich bei SIGTERM sauber (offene Requests
  bekommen 5 s).
- `python -m agent start|dev`: LiveKit-Worker. Ohne VOICEHOOK_HTTP_DISABLED=1 startet
  zusätzlich uvicorn in einem Hintergrund-Thread (lokale Entwicklung, ein Prozess).
  Auf der Box laufen die Worker als voicehook-agent@blue/green mit HTTP_DISABLED=1,
  damit zwei Farben gleichzeitig laufen können (Blue/Green-Deploy, docs/DEPLOY.md).
"""

from __future__ import annotations

import logging
import os
import sys
import threading

import uvicorn


def _serve_http() -> None:
    from .server import app

    port = int(os.environ.get("VOICEHOOK_HTTP_PORT", "7400"))
    host = os.environ.get("VOICEHOOK_HTTP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_config=None, timeout_graceful_shutdown=5)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if sys.argv[1:2] == ["http"]:
        _serve_http()
        return
    from .worker import main as run_worker

    if os.environ.get("VOICEHOOK_HTTP_DISABLED", "") != "1":
        threading.Thread(target=_serve_http, daemon=True, name="vh-http").start()
    run_worker()  # blocks until worker exits; `start` drains on SIGTERM


if __name__ == "__main__":
    main()
