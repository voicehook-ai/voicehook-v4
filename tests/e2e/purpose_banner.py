#!/usr/bin/env python3
"""Offline E2E for the purpose hint (#vh-purpose) in web/voice.html. Zero cost.

Serves web/ from a local http.server on a free port; /api/* is answered with {}
in the browser, nothing reaches voicehook.ai or LiveKit.

Checks per width (default 320, 390, 1280; plus a touch phone at 390 for the
html.vh-phone layout):
  * visible on first load, DE text from the i18n table
  * no bounding-box overlap with the header (logo, top buttons, #vhm-head)
    and header boxes unchanged by the hint (nothing pushed)
  * hidden while a call runs (#vh-presence.vh-in-call), back after it
  * X hides it, stays hidden after reload, back after the localStorage key is removed
  * localStorage throwing: still visible and dismissable
  * 0 console errors

Exit: 0 all checks pass, 1 a check failed.

Usage:
  timeout 90 python3 tests/e2e/purpose_banner.py
  timeout 60 python3 tests/e2e/purpose_banner.py --width 390 --shots /tmp/shots
"""
import argparse
import contextlib
import functools
import http.server
import os
import sys
import threading

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "..", "web"))
KEY = "vh-purpose-dismissed-v1"
HEAD = ["label", "btn-info", "btn-theme", "btn-vh-design", "btn-vh-account", "vhm-head"]
DE_START = "voicehook.ai: Sprich mit deinen Agenten."
EN_START = "voicehook.ai: Talk with your agents."

BOX_JS = """id => { const e = document.getElementById(id); if (!e || e.hidden) return null;
  if (getComputedStyle(e).display === 'none') return null;
  const b = e.getBoundingClientRect(); return b.width ? [b.x, b.y, b.width, b.height] : null; }"""

fails = []


def check(ok, msg):
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}", flush=True)
    if not ok:
        fails.append(msg)


def text(page):
    loc = page.locator("#vh-purpose p")
    return loc.inner_text() if loc.count() else ""


def overlap(a, b):
    return bool(a and b and a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])


@contextlib.contextmanager
def serve(web):
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=web)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}/voice.html"
    finally:
        srv.shutdown()


def new_page(browser, url, kw, locale="de-DE", init=None):
    ctx = browser.new_context(locale=locale, **kw)
    if init:
        ctx.add_init_script(init)
    ctx.route("**/api/**", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    page = ctx.new_page()
    errs = []
    page.on("console", lambda m: m.type == "error" and errs.append(m.text))
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(url)
    page.wait_for_timeout(1200)
    return ctx, page, errs


def run_case(browser, url, name, kw, shots):
    print(f"[{name}]")
    ctx, page, errs = new_page(browser, url, kw)
    vis = page.is_visible("#vh-purpose")
    check(vis, "visible on first load")
    check(text(page).startswith(DE_START), "DE text via i18n")
    bb = page.evaluate(BOX_JS, "vh-purpose")
    heads = {h: page.evaluate(BOX_JS, h) for h in HEAD}
    hit = [h for h in HEAD if overlap(bb, heads[h])]
    check(not hit, f"no overlap with header {hit or ''} box={bb and [round(v) for v in bb]}")
    if shots:
        page.screenshot(path=os.path.join(shots, f"purpose-{name}.png"))
    page.evaluate("(document.getElementById('vh-purpose') || {}).hidden = true")
    page.wait_for_timeout(50)
    check(heads == {h: page.evaluate(BOX_JS, h) for h in HEAD}, "header not moved by the hint")
    page.reload()
    page.wait_for_timeout(800)
    page.evaluate("document.getElementById('vh-presence').classList.add('vh-in-call')")
    page.wait_for_timeout(50)
    check(not page.is_visible("#vh-purpose"), "hidden during a call")
    page.evaluate("document.getElementById('vh-presence').classList.remove('vh-in-call')")
    page.wait_for_timeout(50)
    check(page.is_visible("#vh-purpose"), "back after the call")
    if page.locator("#vh-purpose-x").count():
        page.click("#vh-purpose-x")
    check(not page.is_visible("#vh-purpose"), "hidden after X")
    page.reload()
    page.wait_for_timeout(800)
    check(not page.is_visible("#vh-purpose"), "still hidden after reload")
    page.evaluate(f"localStorage.removeItem('{KEY}')")
    page.reload()
    page.wait_for_timeout(800)
    check(page.is_visible("#vh-purpose"), "visible again after removing the key")
    check(not errs, f"0 console errors {errs or ''}")
    ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, action="append", help="default: 320 390 1280 + phone 390")
    ap.add_argument("--shots", help="directory for screenshots")
    ap.add_argument("--web", default=WEB, help="web root to serve (negative control: an old checkout)")
    a = ap.parse_args()
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)
    heights = {320: 640, 390: 844, 1280: 800}
    cases = [(str(w), {"viewport": {"width": w, "height": heights.get(w, 800)}}) for w in (a.width or [320, 390, 1280])]
    if not a.width:
        cases.append(("390-phone", {"viewport": {"width": 390, "height": 844}, "screen": {"width": 390, "height": 844},
                                    "is_mobile": True, "has_touch": True}))
    with serve(a.web) as url, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, kw in cases:
                run_case(browser, url, name, kw, a.shots)
            print("[no-localStorage, en]")
            ctx, page, errs = new_page(browser, url, {"viewport": {"width": 390, "height": 844}}, locale="en-US",
                                       init="Object.defineProperty(window,'localStorage',{get(){throw new Error('blocked')}})")
            check(page.is_visible("#vh-purpose"), "visible without localStorage")
            check(text(page).startswith(EN_START), "EN text via i18n")
            if page.locator("#vh-purpose-x").count():
                page.click("#vh-purpose-x")
            check(not page.is_visible("#vh-purpose"), "dismissable without localStorage")
            check(not [e for e in errs if "purpose" in e.lower()], "no purpose-related errors")
            ctx.close()
        finally:
            browser.close()
    print(f"{'OK' if not fails else 'FAILED'}: {len(fails)} failed check(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
