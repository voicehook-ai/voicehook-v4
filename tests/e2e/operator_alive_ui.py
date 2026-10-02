"""E2E (headless Chromium, gemockter LiveKit-Raum): Agent-Chip zeigt, ob der Agent zuhört.

Oliver 02.10.: "Wenn Claude nicht mehr drin ist, muss das im Call sichtbar werden!!!!!"
Topic operator.alive (CLI ab 0.8.0, alle 10 s solange next/say bedient werden).
Je Viewport 390 und 1280:
  legacy  Agent joint ohne Lebenszeichen (alte CLI)         -> Chip normal
  normal  alive:true                                        -> Chip normal, kein Puls
  dimmed  > 20 s ohne Lebenszeichen (Uhr vorgestellt)       -> gedimmt + "hört gerade nicht zu"
  back    frisches alive:true (Positivkontrolle)            -> wieder normal
  off     alive:false                                       -> sofort gedimmt
  gone    Agent verlässt den Raum                           -> Chip weg
0 Console-Errors. Lauf: python3 tests/e2e/operator_alive_ui.py [shot_dir]  (Exit 0 = grün)
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parents[2] / "web"

FAKE_LK = r"""
(() => {
  class Emitter { constructor(){ this._h = {}; } on(e, f){ (this._h[e] = this._h[e] || []).push(f); return this; }
    off(){ return this; } emit(e, ...a){ (this._h[e] || []).forEach(f => { try { f(...a); } catch (x) { console.error(x); } }); } }
  const RoomEvent = new Proxy({}, { get: (_, k) => String(k) });
  const Track = { Kind: { Audio: 'audio', Video: 'video' }, Source: { Microphone: 'microphone' } };
  class Room extends Emitter {
    constructor(){ super(); this.remoteParticipants = new Map(); this.numParticipants = 1; this.canPlaybackAudio = true;
      this.state = 'disconnected'; this.micOn = false;
      this.localParticipant = { identity: 'host-x', attributes: {}, setMicrophoneEnabled: async (on) => { this.micOn = !!on; },
        publishData: async () => {}, audioTrackPublications: new Map(), trackPublications: new Map() };
      window.__fakeRooms = (window.__fakeRooms || []); window.__fakeRooms.push(this); }
    async connect(){ this.state = 'connected'; setTimeout(() => this.emit('Connected'), 0); }
    async startAudio(){}
    disconnect(){ if (this.state === 'disconnected') return; this.state = 'disconnected'; this.micOn = false; this.emit('Disconnected', 'CLIENT_INITIATED'); }
    // Test-Hilfen
    _agentJoin(id){ const p = { identity: id, attributes: {}, kind: 'AGENT', trackPublications: new Map(), audioTrackPublications: new Map() };
      this.remoteParticipants.set(id, p); this.numParticipants = this.remoteParticipants.size + 1; this.emit('ParticipantConnected', p); return p; }
    _agentLeave(id){ const p = this.remoteParticipants.get(id); this.remoteParticipants.delete(id);
      this.numParticipants = this.remoteParticipants.size + 1; this.emit('ParticipantDisconnected', p); }
    _data(id, topic, obj){ const p = this.remoteParticipants.get(id) || { identity: id };
      this.emit('DataReceived', new TextEncoder().encode(JSON.stringify(obj)), p, 0, topic); }
    _opJoin(id, name){ const p = { identity: id, name: name, attributes: { 'vh.role': 'agent', 'vh.name': name }, kind: 'STANDARD',
        trackPublications: new Map(), audioTrackPublications: new Map() };
      this.remoteParticipants.set(id, p); this.numParticipants = this.remoteParticipants.size + 1; this.emit('ParticipantConnected', p); return p; }
    _serverDelete(){ this.state = 'disconnected'; this.micOn = false; this.emit('Disconnected', 'ROOM_DELETED'); }
  }
  window.LivekitClient = { Room, RoomEvent, Track, DisconnectReason: {}, ConnectionState: {} };
})();
"""

API = {
    "/api/me": {"free": {"eur_left": 0.67, "eur_per_day": 0.3}, "balance_eur": None, "email_masked": None},
    "/api/free/remaining": {"enabled": True, "eur_left": 0.67, "eur_per_day": 0.3},
    "/api/host-call": {"token": "t", "url": "wss://rtc.test", "room": "calm-ember-tide-4PFD", "identity": "host-x"},
    "/api/live/status": {"available": False},
    "/api/billing/config": {"normal": 1.0, "live": 2.0},
}


class _H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(WEB), **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/r/calm-ember-tide-4PFD"):
            self.path = "/voice.html"
        return super().do_GET()


def _serve() -> tuple[http.server.ThreadingHTTPServer, str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _route(page) -> None:
    def api(route):
        path = route.request.url.split("://", 1)[1].split("/", 1)[1].split("?")[0]
        body = API.get("/" + path)
        if body is None:
            return route.fulfill(status=404, body="{}", content_type="application/json")
        return route.fulfill(status=200, body=json.dumps(body), content_type="application/json")
    page.route("**/api/**", api)
    page.route("**/livekit-client*", lambda r: r.fulfill(status=200, body=FAKE_LK,
                                                          content_type="application/javascript"))


def _start_call(page, base: str) -> None:
    page.goto(base + "/r/calm-ember-tide-4PFD?go=1")
    page.wait_for_selector("#vh-join-gate", timeout=15000)
    page.click("#vh-join-gate")
    page.wait_for_function("() => window.lkRoom && window.lkRoom.state === 'connected'", timeout=10000)
    page.evaluate("() => window.lkRoom._agentJoin('agent-AJ_test1')")
    page.wait_for_function("() => window.lkRoom && window.lkRoom.micOn", timeout=5000)



OP = "claude-crown-eb67"


def _chip(page) -> dict:
    return page.evaluate("""() => { const el = document.querySelector('#vh-presence .vh-pres-chip[data-slot="agent"]');
        const cs = getComputedStyle(el); const sub = el.querySelector('.vh-pres-sub');
        return { shown: cs.display !== 'none', deaf: el.classList.contains('deaf'), speak: el.classList.contains('speak'),
                 opacity: +cs.opacity, label: el.querySelector('.vh-pres-label').textContent,
                 sub: sub && getComputedStyle(sub).display !== 'none' ? sub.textContent : '' }; }""")


def _alive(page, alive=True):
    page.evaluate(f"() => window.lkRoom._data('{OP}', 'operator.alive', {{alive: {str(alive).lower()}, ts: Date.now()/1000, idle_s: 1}})")


def _shot(page, d, name):
    if d:
        page.locator("#vh-presence").screenshot(path=os.path.join(d, name))


def run(shot_dir: str | None) -> int:
    srv, base = _serve()
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
        for w, h in ((390, 844), (1280, 800)):
            ctx = browser.new_context(permissions=["microphone"], locale="de-DE", viewport={"width": w, "height": h})
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m, errors=errors: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            _route(page)
            try:
                _start_call(page, base)
                page.evaluate(f"() => window.lkRoom._opJoin('{OP}', 'Claude')")
                page.wait_for_timeout(300)
                c = _chip(page)
                assert c["shown"] and not c["deaf"] and c["label"] == "Claude", ("legacy", c)
                _shot(page, shot_dir, f"{w}_1_legacy.png")
                _alive(page)
                page.wait_for_timeout(200)
                c = _chip(page)
                assert c["shown"] and not c["deaf"] and not c["speak"] and c["opacity"] == 1, ("normal", c)
                _shot(page, shot_dir, f"{w}_2_normal.png")
                # Uhr 25 s vor: kein Lebenszeichen mehr (verwaister Join)
                page.evaluate("() => { const o = Date.now.bind(Date); Date.now = () => o() + 25000; }")
                page.wait_for_function("() => document.querySelector('#vh-presence .vh-pres-chip[data-slot=agent]').classList.contains('deaf')", timeout=5000)
                c = _chip(page)
                assert c["shown"] and c["deaf"] and c["opacity"] < 0.6 and c["sub"] == "hört gerade nicht zu", ("dimmed", c)
                _shot(page, shot_dir, f"{w}_3_dimmed.png")
                _alive(page)  # Positivkontrolle: frisches Lebenszeichen -> wieder normal
                page.wait_for_timeout(200)
                c = _chip(page)
                assert not c["deaf"] and c["sub"] == "" and c["opacity"] == 1, ("back", c)
                _alive(page, False)
                page.wait_for_timeout(200)
                c = _chip(page)
                assert c["deaf"] and c["sub"] == "hört gerade nicht zu", ("off", c)
                rows = page.evaluate("() => document.getElementById('vh-transcript') ? document.getElementById('vh-transcript').textContent : ''")
                assert "operator.alive" not in rows and '"alive"' not in rows, "Lebenszeichen im Transkript"
                page.evaluate(f"() => window.lkRoom._agentLeave('{OP}')")
                page.wait_for_timeout(300)
                c = _chip(page)
                assert not c["shown"] and not c["deaf"], ("gone", c)
                _shot(page, shot_dir, f"{w}_4_gone.png")
                assert not errors, f"Console-Errors: {errors}"
                results.append((w, True, "legacy/normal/dimmed/back/off/gone ok, 0 Console-Errors"))
            except Exception as e:  # noqa: BLE001
                results.append((w, False, f"{e} | console={errors}"))
            finally:
                ctx.close()
        browser.close()
    srv.shutdown()
    for w, good, detail in results:
        print(f"{'PASS' if good else 'FAIL'} {w}px: {detail}")
    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else None
    if d:
        Path(d).mkdir(parents=True, exist_ok=True)
    sys.exit(run(d))
