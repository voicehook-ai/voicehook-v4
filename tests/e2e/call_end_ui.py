"""E2E (headless Chromium, gemockter LiveKit-Raum): Server beendet den Call -> UI beendet ihn.

Oliver 02.10. (iPhone): "der Agent ... geht aus dem Call. Jetzt bin ich alleine da und warte.
Das Frontend muss mich dann auch rauswerfen."

Szenarien (je frische Seite, web/voice.html lokal ausgeliefert, /api/* gemockt,
livekit-client durch einen Fake ersetzt):
  free_limit   Worker sendet call_end {reason: free_limit}, verlässt den Raum
               -> UI "vor dem Call" in <= 3 s, Meldung "Gratis heute aufgebraucht", Mikro aus
  agent_gone   voice-ai verlässt den Raum ohne Topic, kommt nicht wieder -> sofort Hinweis
               "Delta verbindet neu …", Call läuft weiter, Ende erst nach 30 s mit "Call beendet"
  reconnect    voice-ai geht weg und ist nach 10 s wieder da (Re-Dispatch, #128) -> Hinweis
               verschwindet, Call läuft auch über die 30-s-Marke hinaus weiter
  room_deleted Raum gelöscht (Disconnected) -> UI beendet, "Call beendet"
  free_state   Topic free.state im Call -> Gratis-Zeile zeigt den Worker-Wert
Jeweils 0 Console-Errors. Lauf: python3 tests/e2e/call_end_ui.py   (Exit 0 = grün)
"""

from __future__ import annotations

import functools
import http.server
import json
import sys
import threading
import time
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
        elif self.path.split("?")[0] == "/aufladen":  # Caddy-Rewrite (Overlay-iframe aus #127)
            self.path = "/aufladen.html"
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


def _ended(page) -> bool:
    return page.evaluate("""() => !document.getElementById('btn-vh-call').classList.contains('on')
        && (window.__fakeRooms || []).every(r => !r.micOn && r.state === 'disconnected')""")


def _wait_ended(page, limit_s: float) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        if _ended(page):
            return time.monotonic() - t0
        time.sleep(0.05)
    raise AssertionError(f"UI hat den Call nicht binnen {limit_s}s beendet")


def _err_text(page) -> str:
    return page.evaluate("() => { const b = document.getElementById('vh-invite-banner');"
                         " return b && !b.hidden ? b.textContent : ''; }")


def run() -> int:
    srv, base = _serve()
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])

        def scenario(name):
            def deco(fn):
                @functools.wraps(fn)
                def wrapped():
                    ctx = browser.new_context(permissions=["microphone"], locale="de-DE")
                    page = ctx.new_page()
                    errors = []
                    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    _route(page)
                    try:
                        detail = fn(page)
                        assert not errors, f"Console-Errors: {errors}"
                        results.append((name, True, detail))
                    except Exception as e:  # noqa: BLE001
                        results.append((name, False, f"{e} | console={errors}"))
                    finally:
                        ctx.close()
                return wrapped
            return deco

        @scenario("free_limit")
        def _free_limit(page):
            _start_call(page, base)
            page.evaluate("""() => { const r = window.lkRoom;
                r._data('agent-AJ_test1', 'call_end', {reason: 'free_limit'});
                r._agentLeave('agent-AJ_test1'); }""")
            dt = _wait_ended(page, 3.0)
            txt = _err_text(page)
            assert "Gratis heute aufgebraucht" in txt, txt
            assert "Aufladen" in txt, txt
            return f"beendet nach {dt:.2f}s, Meldung: {txt.strip()[:80]}"

        @scenario("agent_gone")
        def _agent_gone(page):
            _start_call(page, base)
            page.evaluate("() => window.lkRoom._agentLeave('agent-AJ_test1')")
            time.sleep(1.0)
            hint = _err_text(page)
            assert "Delta verbindet neu" in hint, hint
            time.sleep(26.0)  # 27 s nach Weggang: noch im Call (Abstimmung #126/#128: 30 s)
            assert not _ended(page), "zu früh beendet (30-s-Karenz)"
            assert "Delta verbindet neu" in _err_text(page), _err_text(page)
            dt = _wait_ended(page, 6.0)
            txt = _err_text(page)
            assert "Call beendet" in txt, txt
            return f"Hinweis sofort, beendet {dt + 27:.1f}s nach Agent-Weggang, Meldung: {txt.strip()[:40]}"

        @scenario("reconnect")
        def _reconnect(page):
            _start_call(page, base)
            page.evaluate("() => window.lkRoom._agentLeave('agent-AJ_test1')")
            time.sleep(1.0)
            assert "Delta verbindet neu" in _err_text(page), _err_text(page)
            time.sleep(9.0)
            page.evaluate("() => window.lkRoom._agentJoin('agent-AJ_test2')")
            time.sleep(0.5)
            assert "Delta verbindet neu" not in _err_text(page), "Hinweis bleibt nach Rückkehr stehen"
            time.sleep(22.0)  # über die 30-s-Marke seit Weggang hinaus
            assert not _ended(page), "Call wurde trotz Agent-Rückkehr beendet"
            return "Hinweis weg nach Rückkehr (10 s), Call läuft nach 32 s weiter"

        @scenario("room_deleted")
        def _room_deleted(page):
            _start_call(page, base)
            page.evaluate("() => window.lkRoom._serverDelete()")
            dt = _wait_ended(page, 3.0)
            txt = _err_text(page)
            assert "Call beendet" in txt, txt
            return f"beendet nach {dt:.2f}s"

        @scenario("free_state")
        def _free_state(page):
            _start_call(page, base)
            page.evaluate("""() => window.lkRoom._data('agent-AJ_test1', 'free.state',
                {eur_left: 0.12, eur_per_day: 0.3, exempt: false, reason: null})""")
            page.wait_for_function("() => /0,12/.test(document.querySelector('.vh-free-left').textContent)", timeout=3000)
            return "Gratis-Zeile zeigt Worker-Wert 0,12 statt /api/me 0,67"

        for fn in (_free_limit, _agent_gone, _reconnect, _room_deleted, _free_state):
            fn()
        browser.close()
    srv.shutdown()
    ok = all(r[1] for r in results)
    for name, good, detail in results:
        print(f"{'PASS' if good else 'FAIL'} {name}: {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
