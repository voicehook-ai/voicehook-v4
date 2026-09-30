#!/usr/bin/env python3
"""Real-backend E2E for web/voice.html against PROD (https://voicehook.ai + LiveKit).

Only the /r/<slug> HTML document is swapped for a LOCAL file (--html) via page.route,
so a change can be tested without deploying. Everything else (API, LiveKit, voice-ai)
is real and COSTS MONEY while a room is alive.

COST RULE (hard): no call may run unbounded, test calls included.
  * internal deadline (--deadline, default 90s, max 120s): SIGALRM aborts the run,
    a watchdog thread SIGKILLs the observer process group and hard-exits the
    process 15s later if teardown hangs. Run it under an outer `timeout 120` too.
  * teardown in try/finally: browser closed, observer killed by its own PGID
    (never pkill -f), observer runs with --no-keep-alive (no reconnect).
  * preflight: the box-local room audit (lk_rooms.sh via SSH) must work BEFORE any
    room is created; if it does not, the run aborts with exit 3 and creates nothing.
  * post-run: lk_rooms.sh --room <slug> is polled for <=60s; the room must be gone
    or have 0 participants. Otherwise the run FAILS and the room is deleted
    (DeleteRoom only if no human participant is left).

Scenarios:
  menulink  connect, open #vh-burger, click "Aufladen".
            --expect keep: new tab, URL unchanged, still connected, observer sees no
                           host peer-left (fixed behaviour, 0606f7a)
            --expect drop: same-tab navigation ends the call (positive control
                           against the unfixed live page)
  joingate  #68/#58: with ?go=1 the gate is visible and NOTHING connects (no
            /api/token, no LiveKit) before a REAL click; after the click the page is
            connected and the observer sees the host's audio-track-on.
            --offline: zero-cost variant, /api/* and LiveKit are blocked in the
            browser, no observer, no room; checks the pre-click half and that the
            click triggers the token request.

Exit: 0 all checks pass, 1 check failed, 3 preflight/setup failed (no room created).

Usage:
  timeout 120 python3 tests/e2e/real_call.py --scenario joingate --width 390
  timeout 120 python3 tests/e2e/real_call.py --scenario menulink --expect keep --width 1280
  timeout 120 python3 tests/e2e/real_call.py --scenario menulink --expect drop \
      --html /path/to/live_voice.html --width 390
  timeout 60  python3 tests/e2e/real_call.py --scenario joingate --offline
"""
import argparse
import contextlib
import os
import queue
import random
import re
import signal
import string
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
LK_ROOMS = os.path.join(HERE, "lk_rooms.sh")
BASE = "https://voicehook.ai"
RTC_HOST = "rtc.voicehook.ai"
AGENT_BIN = os.path.expanduser(os.environ.get("VOICEHOOK_AGENT_BIN", "~/.local/bin/voicehook-agent"))
WORDS = ["fresh", "signal", "clear", "drift", "bright", "swift", "calm", "bold", "lucid", "keen"]
MAX_DEADLINE = 120
POST_CHECK_S = 60


class Deadline(Exception):
    pass


class Abort(Exception):
    pass


def log(msg):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mk_slug():
    # must match voice.html VH_SLUG_RX (3 lowercase words + code); "etest" marks test rooms
    w = "-".join(random.choice(WORDS) for _ in range(2))
    return f"etest-{w}-{''.join(random.choices(string.ascii_uppercase + string.digits, k=5))}"


def mint_invite(slug, mode):
    if mode == "peer":
        return "1"
    out = subprocess.run(
        ["p2ai", "run", "-e", "INVITE_SECRET=voicehook_v4_invite_secret", "--",
         "python3", "-m", "agent.cli", "invite", slug, "--base", BASE],
        cwd=f"{REPO}/apps", capture_output=True, text=True, timeout=60)
    m = re.search(r"[?&]invite=([^&\s]+)", out.stdout)
    if out.returncode != 0 or not m:
        raise SystemExit(f"invite mint failed rc={out.returncode} (stderr len={len(out.stderr)})")
    return m.group(1)  # never printed


def lk_room_state(slug=None, delete=False, timeout=110):
    """Run the box-local audit. Returns (rc, dict|None, text)."""
    cmd = ["timeout", str(timeout), "bash", LK_ROOMS]
    if slug:
        cmd += ["--room", slug] + (["--delete-room"] if delete else [])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        return 124, None, "lk_rooms timeout"
    txt = (p.stdout + p.stderr).strip()
    states = re.findall(r"ROOMSTATE name=(\S+) present=(\d) participants=(\d+) humans=(\d+)", p.stdout)
    st = None
    if states:
        n, pr, pa, hu = states[-1]
        st = {"name": n, "present": int(pr), "participants": int(pa), "humans": int(hu)}
    return p.returncode, st, txt


class Observer:
    def __init__(self, slug):
        self.ident = "e2e-observer-" + "".join(random.choices(string.ascii_lowercase, k=5))
        self.lines = []
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            [AGENT_BIN, "join", f"{BASE}/r/{slug}", "--json", "--no-greet", "--no-keep-alive",
             "--identity", self.ident],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            self.lines.append((time.time(), line))
            self.q.put(line)

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        for _, l in list(self.lines):
            if pred(l):
                return l
        while time.time() < end:
            try:
                l = self.q.get(timeout=max(0.1, end - time.time()))
            except queue.Empty:
                break
            if pred(l):
                return l
        return None

    def saw(self, needle, since=0.0):
        return [l for t, l in list(self.lines) if t >= since and needle in l]

    def kill_hard(self):
        with contextlib.suppress(Exception):
            os.killpg(self.proc.pid, signal.SIGKILL)  # own process group only

    def stop(self):
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            with contextlib.suppress(Exception):
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.kill_hard()
                self.proc.wait(timeout=5)
        log(f"observer pid={self.proc.pid} stopped rc={self.proc.returncode}")


def connected_js():
    return """() => {
      const r = window.lkRoom, m = document.getElementById('btn-mute');
      return !!(r && r.state === 'connected' && m && !m.hasAttribute('hidden'));
    }"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default=os.path.join(REPO, "web", "voice.html"))
    ap.add_argument("--scenario", choices=["menulink", "joingate"], required=True)
    ap.add_argument("--expect", choices=["keep", "drop"], default="keep",
                    help="menulink only: keep = call survives the link click; drop = it ends")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--invite", choices=["hmac", "peer"], default="hmac")
    ap.add_argument("--deadline", type=int, default=90, help=f"hard wall-clock cap, max {MAX_DEADLINE}s")
    ap.add_argument("--offline", action="store_true",
                    help="joingate only: block /api + LiveKit, create no room (0 cost)")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    if a.deadline <= 0 or a.deadline > MAX_DEADLINE:
        raise SystemExit(f"--deadline must be 1..{MAX_DEADLINE}")
    if a.offline and a.scenario != "joingate":
        raise SystemExit("--offline is only supported for --scenario joingate")
    if not os.path.exists(a.html):
        raise SystemExit(f"missing {a.html}")
    with open(a.html, "rb") as fh:
        html = fh.read()
    height = 844 if a.width <= 480 else 800
    slug = mk_slug()

    # -- preflight: never create a room we cannot verify afterwards --------------
    if not a.offline:
        rc, st, txt = lk_room_state(slug)
        if rc != 0 or st is None:
            print(f"PREFLIGHT FAIL room audit unavailable (rc={rc}): {txt[-300:]}")
            print("RESULT ABORT no room created")
            return 3
        log(f"preflight: room audit OK ({slug} present={st['present']})")

    invite = "offline" if a.offline else mint_invite(slug, a.invite)
    print(f"== scenario={a.scenario} expect={a.expect} offline={a.offline} viewport={a.width}x{height} "
          f"room={slug} deadline={a.deadline}s file={a.html}", flush=True)

    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)) if detail else ''}", flush=True)

    # -- deadline: SIGALRM + watchdog thread ------------------------------------
    obs = None
    state = {"done": False}

    def on_alarm(signum, frame):
        raise Deadline(f"internal deadline {a.deadline}s hit")

    def watchdog():
        end = time.time() + a.deadline + 15
        while time.time() < end:
            if state["done"]:
                return
            time.sleep(0.5)
        if not state["done"]:
            print("WATCHDOG deadline+15s: killing observer pgid and hard-exiting", flush=True)
            if obs:
                obs.kill_hard()
            os._exit(4)

    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(a.deadline)
    threading.Thread(target=watchdog, daemon=True).start()

    console_errors, served, api_reqs, rtc_reqs = [], [], [], []
    browser = None
    created_room = not a.offline
    try:
        from playwright.sync_api import sync_playwright
        if not a.offline:
            obs = Observer(slug)
            l = obs.wait_for(lambda s: "connected" in s and "peers" in s, 30)
            log(f"observer pid={obs.proc.pid} id={obs.ident}: {l or 'NO CONNECT'}")
            if not l:
                check("observer connected", False, "\n".join(x for _, x in obs.lines[-10:]))
                raise Abort("observer did not connect")
        with sync_playwright() as p:
            args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required"]
            if a.offline:
                args.append(f"--host-resolver-rules=MAP {RTC_HOST} ~NOTFOUND")
            browser = p.chromium.launch(headless=not a.headed, args=args)
            ctx = browser.new_context(viewport={"width": a.width, "height": height},
                                      permissions=["microphone"])

            def route_all(route):
                req = route.request
                u = req.url
                if req.resource_type == "document" and re.match(rf"^{re.escape(BASE)}/r/[^/?]+", u):
                    served.append(u.split("?")[0])
                    return route.fulfill(status=200, body=html,
                                         headers={"content-type": "text/html; charset=utf-8",
                                                  "cache-control": "no-store"})
                if u.startswith(f"{BASE}/api/"):
                    api_reqs.append((time.time(), u.split("?")[0]))
                    if a.offline:
                        return route.abort()
                if RTC_HOST in u:
                    rtc_reqs.append((time.time(), u.split("?")[0]))
                    if a.offline:
                        return route.abort()
                return route.continue_()

            ctx.route("**/*", route_all)
            new_pages = []
            ctx.on("page", lambda pg: new_pages.append(pg))
            page = ctx.new_page()
            page.on("console", lambda m: m.type == "error" and console_errors.append(m.text))
            page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
            page.on("websocket", lambda ws: rtc_reqs.append((time.time(), "ws:" + ws.url.split("?")[0])))

            page.goto(f"{BASE}/r/{slug}?invite={invite}&go=1", wait_until="load")
            check("precondition: /r/<slug> doc served from local file (1x)", len(served) == 1, served)

            gate = page.locator("#vh-join-gate")
            try:
                gate.wait_for(state="visible", timeout=8000)
                has_gate = True
            except Exception:
                has_gate = False  # pre-#68 page: ?go=1 autoconnects without a gate
            log(f"join gate present: {has_gate}")

            if a.scenario == "joingate":
                page.wait_for_timeout(3000)  # give a buggy autoconnect time to fire
                pre_api = [u for _, u in api_reqs if "/api/token" in u or "/api/host-call" in u]
                check("joingate: gate visible before click", has_gate)
                check("joingate: NOT connected before click", not page.evaluate(connected_js()))
                check("joingate: no token/host-call request before click", not pre_api, pre_api)
                check("joingate: no LiveKit connection before click", not rtc_reqs, rtc_reqs[:3])
                if obs:
                    host_seen = [l for _, l in obs.lines if "peer-joined" in l and obs.ident not in l
                                 and "voice-ai" not in l]
                    check("joingate: observer saw no host join before click", not host_seen, host_seen[:2])
                if not has_gate:
                    raise Abort("no join gate to click")
                t_click = time.time()
                gate.click()  # REAL user gesture (#58)
                if a.offline:
                    end = time.time() + 10
                    while time.time() < end and not any(t >= t_click for t, _ in api_reqs):
                        page.wait_for_timeout(200)
                    post = [u for t, u in api_reqs if t >= t_click]
                    check("joingate(offline): click triggers token request", bool(post), post[:2])
                    check("joingate(offline): gate removed after click",
                          page.locator("#vh-join-gate").count() == 0)
                else:
                    try:
                        page.wait_for_function(connected_js(), timeout=30000)
                        conn = True
                    except Exception:
                        conn = False
                    check("joingate: connected after real click", conn, page.evaluate(
                        "() => (document.getElementById('err')||{}).textContent || ''"))
                    host_id = page.evaluate(f"() => localStorage.getItem('vh-id:v1:{slug}')")
                    seen = obs.wait_for(lambda s: f"audio-track-on: {host_id}" in s, 20) if host_id else None
                    check("joingate: observer sees host audio-track-on", bool(seen), seen or f"host={host_id}")
            else:  # menulink
                if has_gate:
                    gate.click()  # real user gesture
                try:
                    page.wait_for_function(connected_js(), timeout=30000)
                    conn0 = True
                except Exception:
                    conn0 = False
                host_id = page.evaluate(f"() => localStorage.getItem('vh-id:v1:{slug}')")
                url_before = page.url
                log(f"connected={conn0} host_identity={host_id}")
                check("precondition: connected via real prod LiveKit", conn0, page.evaluate(
                    "() => (document.getElementById('err')||{}).textContent || ''"))
                seen = obs.wait_for(lambda s: f"peer-joined: {host_id}" in s, 20)
                check("precondition: observer saw host join", bool(seen), seen or "")
                if not conn0:
                    raise Abort("host not connected")
                page.wait_for_timeout(1500)
                page.evaluate("() => { window.__e2eMarker = 'alive-' + Date.now(); }")
                marker = page.evaluate("() => window.__e2eMarker")
                t_click = time.time()
                page.locator("#vh-burger").click()
                page.locator("#vh-foot-menu").wait_for(state="visible", timeout=5000)
                page.locator("#vh-foot-menu a", has_text="Aufladen").click()
                page.wait_for_timeout(5000)
                opened = [pg for pg in new_pages if pg is not page]
                new_url = ""
                if opened:
                    with contextlib.suppress(Exception):
                        opened[0].wait_for_load_state("domcontentloaded", timeout=10000)
                    new_url = opened[0].url
                try:
                    still = bool(page.evaluate(connected_js()))
                except Exception:
                    still = False
                try:
                    same_doc = page.evaluate("() => window.__e2eMarker") == marker
                except Exception:
                    same_doc = False
                left = obs.saw(f"peer-left: {host_id}", since=t_click)
                if not left and not still:
                    end = time.time() + 15
                    while time.time() < end and not left:
                        time.sleep(0.2)
                        left = obs.saw(f"peer-left: {host_id}", since=t_click)
                if a.expect == "keep":
                    check("keep: new tab opened for Aufladen", bool(opened) and "/aufladen" in new_url,
                          f"new_tabs={len(opened)} url={new_url or '-'}")
                    check("keep: original page URL unchanged", page.url == url_before)
                    check("keep: still connected 5s after click", still)
                    check("keep: same document (no reload)", same_doc)
                    check("keep: observer did NOT see host peer-left", not left, left[:1])
                else:
                    check("drop: page navigated away / call ended", (not still) or (not same_doc),
                          f"still={still} same_doc={same_doc} url={page.url.split('?')[0]}")
                    check("drop: observer saw host peer-left", bool(left), left[:1])
                # observer control on our own hang-up
                t_td = time.time()
                if still:
                    with contextlib.suppress(Exception):
                        page.evaluate("() => window.lkRoom && window.lkRoom.disconnect()")
                    end = time.time() + 15
                    hit = []
                    while time.time() < end and not hit:
                        time.sleep(0.2)
                        hit = obs.saw(f"peer-left: {host_id}", since=t_td)
                    check("observer-control: host peer-left seen on our hang-up", bool(hit))
            print(f"INFO  console-errors={len(console_errors)}"
                  + "".join(f"\n      - {e[:160]}" for e in console_errors[:8]), flush=True)
            try:
                if page.evaluate("() => !!(window.lkRoom && window.lkRoom.state === 'connected')"):
                    page.evaluate("() => window.lkRoom.disconnect()")
                    page.wait_for_timeout(1000)
            except Exception:
                pass
            browser.close()
            browser = None
    except Deadline as e:
        check("internal deadline", False, str(e))
    except Abort as e:
        print(f"ABORT {e}", flush=True)
        results.append(False)
    except Exception as e:  # any error must still reach teardown + post-run room check
        check("unexpected error", False, f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        signal.alarm(0)
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        if obs:
            obs.stop()
        state["done"] = True

    # -- post-run: room must be gone or empty within 60s -------------------------
    if created_room:
        end = time.time() + POST_CHECK_S
        st, txt = None, ""
        while True:
            rc, st, txt = lk_room_state(slug, timeout=40)
            log(f"post-check {slug}: rc={rc} state={st}")
            if st and (st["present"] == 0 or st["participants"] == 0):
                break
            if time.time() >= end:
                break
            time.sleep(8)
        ok = bool(st) and (st["present"] == 0 or st["participants"] == 0)
        check(f"cost: room {slug} gone/empty within {POST_CHECK_S}s", ok, st or txt[-200:])
        if not ok:
            rc, st2, txt2 = lk_room_state(slug, delete=True, timeout=40)
            print(f"COST  room {slug} not empty -> DeleteRoom attempted: final={st2} rc={rc}")
            print(txt2[-400:])
        print(f"ROOM  {slug} final={st if ok else 'see DeleteRoom above'}")
    ok = all(results) and bool(results)
    print(f"RESULT {'PASS' if ok else 'FAIL'} scenario={a.scenario} expect={a.expect} width={a.width}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
