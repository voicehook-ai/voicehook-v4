"""Single-process entrypoint — runs the FastAPI surface + LiveKit worker.

`python -m agent` starts uvicorn (HTTP :7400) in a background thread, then the
livekit-agents worker registers + blocks. One systemd unit, two responsibilities.
"""

from __future__ import annotations

import logging
import os
import threading

import uvicorn

from .server import app
from .worker import main as run_worker


def _serve_http() -> None:
    port = int(os.environ.get("VOICEHOOK_HTTP_PORT", "7400"))
    host = os.environ.get("VOICEHOOK_HTTP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_config=None)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    threading.Thread(target=_serve_http, daemon=True, name="vh-http").start()
    run_worker()  # blocks until worker exits / SIGTERM


if __name__ == "__main__":
    main()
