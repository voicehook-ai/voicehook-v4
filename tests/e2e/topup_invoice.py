#!/usr/bin/env python3
"""Offline E2E for the invoice checkbox (#invoice) in web/aufladen.html. Zero cost.

Serves web/ from a local http.server on a free port; /api/* is answered in the
browser (billing config with checkout_available, /api/checkout captured), the
Stripe URL is answered with an empty page. Nothing reaches voicehook.ai or Stripe.

Checks per width (default 320, 390, 1280) and mode (normal, ?embed=1, and
?embed=1 inside a same-origin iframe like the overlay in voice.html):
  * checkbox + label visible, inside the viewport, DE text from the i18n table
  * no overlap with the amount chips or the CTA, no horizontal scroll
  * unchecked by default; CTA then sends {"invoice": false}
  * clicking the label checks it; CTA then sends {"invoice": true}
  * 0 console errors
Plus EN locale at 390: English label.

Exit: 0 all checks pass, 1 a check failed.

Usage:
  timeout 120 python3 tests/e2e/topup_invoice.py --shots /tmp/shots
  timeout 60 python3 tests/e2e/topup_invoice.py --width 390
"""
import argparse
import contextlib
import functools
import http.server
import json
import os
import sys
import threading

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.abspath(os.path.join(HERE, "..", "..", "web"))
DE = "Rechnung mit USt-ID (für Unternehmen)"
EN = "Invoice with VAT ID (for businesses)"
STRIPE = "https://checkout.stripe.test/cs_e2e"
CONFIG = {"currency": "EUR", "amounts_eur": [5, 10, 20, 50], "min_eur": 5, "max_eur": 200,
          "checkout_available": True, "approx_eur_per_hour": {"normal": 1, "live": 2}}

BOX_JS = """sel => { const e = document.querySelector(sel); if (!e) return null;
  const b = e.getBoundingClientRect(); return b.width ? [b.x, b.y, b.width, b.height] : null; }"""

fails = []


def check(ok, msg):
    print(f"  {'PASS' if ok else 'FAIL'}  {msg}", flush=True)
    if not ok:
        fails.append(msg)


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
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def new_ctx(browser, base, width, locale, sent):
    ctx = browser.new_context(locale=locale, viewport={"width": width, "height": 860})

    def api(route):
        url = route.request.url
        if url.endswith("/api/billing/config"):
            return route.fulfill(json=CONFIG)
        if url.endswith("/api/checkout"):
            sent.append(json.loads(route.request.post_data or "{}"))
            return route.fulfill(json={"url": STRIPE, "session_id": "cs_e2e"})
        return route.fulfill(status=404, json={})

    ctx.route(f"{base}/api/**", api)
    ctx.route("https://checkout.stripe.test/**",
              lambda r: r.fulfill(content_type="text/html", body="<title>stripe</title>"))
    # Wirt fürs Overlay: gleicher Origin wie voice.html, aufladen.html im iframe.
    ctx.route(f"{base}/host.html", lambda r: r.fulfill(
        content_type="text/html",
        body='<!doctype html><body style="margin:0"><iframe id="f" src="/aufladen.html?embed=1" '
             'style="border:0;width:100vw;height:100vh;display:block"></iframe></body>'))
    return ctx


def run_case(browser, base, width, mode, locale, shots):
    tag = f"{width}px {mode} {locale}"
    print(f"\n== {tag}", flush=True)
    sent, errors = [], []
    ctx = new_ctx(browser, base, width, locale, sent)
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda e: errors.append(str(e)))
    url = {"normal": "/aufladen.html", "embed": "/aufladen.html?embed=1", "iframe": "/host.html"}[mode]
    page.goto(base + url)
    if mode == "iframe":
        page.wait_for_selector("#f")
        fr = page.frame_locator("#f")
        frame = page.frame(url=lambda u: "aufladen.html" in u)
        frame.wait_for_load_state()
        root, scope = frame, fr
    else:
        root, scope = page, page
    root.wait_for_function("() => !document.getElementById('cta').disabled")

    if mode != "normal":
        check(root.evaluate("document.body.classList.contains('embed')"), f"{tag}: body.embed set")

    cb, lbl = scope.locator("#invoice"), scope.locator("label.opt")
    check(cb.is_visible() and lbl.is_visible(), f"{tag}: checkbox + label visible")
    want = DE if locale.startswith("de") else EN
    got = scope.locator("#t-invoice").inner_text()
    check(got == want, f"{tag}: label text {got!r}")
    check(not cb.is_checked(), f"{tag}: unchecked by default")

    vw = root.evaluate("innerWidth")
    lb = root.evaluate(BOX_JS, "label.opt")
    check(lb is not None and lb[0] >= 0 and lb[0] + lb[2] <= vw, f"{tag}: label inside viewport {lb}")
    check(lb is not None and lb[3] >= 36, f"{tag}: tap target height >= 36px")
    check(not overlap(lb, root.evaluate(BOX_JS, "#chips")), f"{tag}: no overlap with chips")
    check(not overlap(lb, root.evaluate(BOX_JS, "#cta")), f"{tag}: no overlap with CTA")
    check(root.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"{tag}: no horizontal scroll")

    if shots:
        page.screenshot(path=os.path.join(shots, f"invoice-{width}-{mode}-{locale}-off.png"), full_page=True)

    def click_cta():
        n = len(sent)
        if mode == "iframe":
            with ctx.expect_page() as pop:
                scope.locator("#cta").click()
            pop.value.close()
            root.wait_for_function("() => !document.getElementById('cta').disabled")
        else:
            scope.locator("#cta").click()
            page.wait_for_url(STRIPE)
        check(len(sent) == n + 1, f"{tag}: one /api/checkout request")
        return sent[-1] if len(sent) > n else {}

    body = click_cta()
    check(body.get("invoice") is False and body.get("amount_eur") == 20, f"{tag}: default sends {body}")

    if mode != "iframe":
        page.goto(base + url)
        root = page
        root.wait_for_function("() => !document.getElementById('cta').disabled")
        check(not scope.locator("#invoice").is_checked(), f"{tag}: unchecked after reload")
    scope.locator("#t-invoice").click()  # Klick auf den Text schaltet die Box
    check(scope.locator("#invoice").is_checked(), f"{tag}: label click checks the box")
    if shots:
        page.screenshot(path=os.path.join(shots, f"invoice-{width}-{mode}-{locale}-on.png"), full_page=True)
    body = click_cta()
    check(body.get("invoice") is True and body.get("amount_eur") == 20, f"{tag}: checked sends {body}")

    check(not errors, f"{tag}: 0 console errors {errors}")
    ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=int, action="append")
    ap.add_argument("--shots")
    a = ap.parse_args()
    widths = a.width or [320, 390, 1280]
    if a.shots:
        os.makedirs(a.shots, exist_ok=True)
    with serve(WEB) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        for w in widths:
            for mode in ("normal", "embed", "iframe"):
                run_case(browser, base, w, mode, "de-DE", a.shots)
        run_case(browser, base, widths[min(1, len(widths) - 1)], "normal", "en-US", a.shots)
        browser.close()
    print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAIL'}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
