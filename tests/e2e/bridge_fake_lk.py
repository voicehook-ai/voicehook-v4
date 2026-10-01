"""Local E2E server for the HTTPS bridge: the real FastAPI app on :PORT with LiveKit
replaced by a scripted fake voice-ai. Nothing leaves the machine.

    python tests/e2e/bridge_fake_lk.py 7499

Script of the fake room (per bridge session):
  * peers: voice-ai (kind agent) and the human host
  * 1 s after join: user turn "Hallo Agent, hörst du mich?"
  * every operator.say is "spoken": echoed back as transcript role=operator
  * the first say WITHOUT mode (and not the greet) additionally triggers an
    operator.revise, like a say that overlapped unspoken text
  * a say with mode=overwrite is followed by the user turn "Danke, tschüss."
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "apps"))

os.environ.setdefault("LIVEKIT_API_KEY", "API_E2E_FAKE")
os.environ.setdefault("LIVEKIT_API_SECRET", "fake-secret-e2e-only")
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:9")  # unreachable on purpose
os.environ.setdefault("INVITE_SECRET", "e2e-invite-secret")
os.environ.setdefault("VOICEHOOK_STATE_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "vh-bridge-e2e"))

import uvicorn  # noqa: E402

from agent import bridge  # noqa: E402
from agent import server as srv  # noqa: E402

srv._ensure_agent_dispatched = lambda *a, **k: None  # no LiveKit twirp calls


class _Pub(SimpleNamespace):
    pass


def _peer(identity: str, kind: int) -> SimpleNamespace:
    return SimpleNamespace(identity=identity, kind=kind, name=identity, attributes={},
                           track_publications={"a": _Pub(kind=1, muted=False)})


class FakeVoiceRoom:
    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}
        self.remote_participants = {"voice-ai-e2e": _peer("voice-ai-e2e", 4),
                                    "oliver-browser": _peer("oliver-browser", 0)}
        self.local_participant = SimpleNamespace(publish_data=self._publish)
        self.revised = False
        self.says = 0

    def on(self, ev, cb=None):
        self.handlers.setdefault(ev, []).append(cb)
        return cb

    def _emit(self, ev, *args):
        for cb in self.handlers.get(ev, []):
            cb(*args)

    def _data(self, topic: str, payload: dict) -> None:
        pkt = SimpleNamespace(topic=topic, data=json.dumps(payload).encode(),
                              participant=SimpleNamespace(identity="voice-ai-e2e"))
        self._emit("data_received", pkt)

    async def connect(self, url, token, options=None):
        if os.environ.get("E2E_FAIL_CONNECT"):
            raise RuntimeError("wait_pc_connection timed out")
        loop = asyncio.get_running_loop()
        loop.call_later(1.0, self._data, "transcript", {"role": "user", "text": "Hallo Agent, hörst du mich?"})
        loop.call_later(1.5, self._data, "agent.heartbeat", {"ts": 1, "healthy": True})

    async def disconnect(self):
        return None

    async def _publish(self, data: bytes, reliable: bool = True, topic: str = ""):
        payload = json.loads(data)
        print(f"[fake-lk] publish topic={topic} keys={sorted(payload)}", flush=True)
        if topic != "operator.say":
            return
        self.says += 1
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, self._data, "transcript", {"role": "operator", "text": payload["text"]})
        mode = payload.get("mode")
        if mode is None and self.says > 1 and not self.revised:
            self.revised = True
            loop.call_later(0.5, self._data, "operator.revise", {
                "unspoken": [payload["text"]], "new": payload["text"],
                "text": f"REVISE: Noch NICHT gesprochen: [1] {payload['text']} Deine neue Aussage: [neu] ..."})
        if mode == "overwrite":
            loop.call_later(1.0, self._data, "transcript", {"role": "user", "text": "Danke, tschüss."})


bridge.ROOM_FACTORY = FakeVoiceRoom
bridge.ROOM_OPTIONS = lambda: None

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7499
    uvicorn.run(srv.app, host="127.0.0.1", port=port, log_level="info")
