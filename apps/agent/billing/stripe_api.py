"""Minimale Stripe-Anbindung ohne SDK (keine neue Abhängigkeit).

- create_checkout_session(): POST https://api.stripe.com/v1/checkout/sessions
  (form-encoded, Bearer STRIPE_SECRET_KEY), https://docs.stripe.com/api/checkout/sessions/create
- verify_signature(): Webhook-Signatur wie in der Stripe-Doku
  "Verify webhook signatures manually" (https://docs.stripe.com/webhooks#verify-manually):
  Header `Stripe-Signature: t=<ts>,v1=<hex>[,v1=...]`, erwartet wird
  HMAC-SHA256(secret, f"{t}.{raw_body}") und |now - t| <= Toleranz (Default 300 s).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

STRIPE_API = "https://api.stripe.com/v1"
DEFAULT_TOLERANCE_S = 300


# Rechnung + USt-ID für eine Aufladung (Checkout, mode=payment), nur wenn der Kunde
# sie auf /aufladen anhakt (CheckoutRequest.invoice): Stripe berechnet jede
# Rechnung extra. Laut Stripe-Doku
# genügen diese beiden Schalter; customer_creation und billing_address_collection
# sind dafür nicht nötig (ohne Customer legt Checkout einen Gastkunden an).
INVOICE_PARAMS: dict = {
    # Nach erfolgreicher Zahlung eine bezahlte Rechnung (PDF) erzeugen und mailen:
    # https://docs.stripe.com/payments/checkout/receipts#paid-invoices
    # https://docs.stripe.com/api/checkout/sessions/create#create_checkout_session-invoice_creation-enabled
    "invoice_creation": {"enabled": "true"},
    # Geschäftskunden können USt-ID + Firmenname angeben (erscheint auf der Rechnung):
    # https://docs.stripe.com/tax/checkout/tax-ids
    # https://docs.stripe.com/api/checkout/sessions/create#create_checkout_session-tax_id_collection-enabled
    "tax_id_collection": {"enabled": "true"},
}


def account_tax_id() -> str:
    """Eigene USt-ID des Verkäufers als Stripe-Tax-ID-Objekt (txi_...), z. B. für DE310620765.

    Kein Secret, aber pro Konto verschieden (Live/Sandbox), deshalb Env statt Konstante.
    Leer = Parameter entfällt, Stripe nimmt dann die Dashboard-Einstellung.
    """
    return os.environ.get("STRIPE_ACCOUNT_TAX_ID", "").strip()


def invoice_params() -> dict:
    """INVOICE_PARAMS (frische Kopie) plus, falls gesetzt, die eigene USt-ID auf der Rechnung.

    invoice_creation.invoice_data.account_tax_ids setzt die Tax-ID des Kontos explizit auf
    die Rechnung, unabhängig von der Dashboard-Voreinstellung:
    https://docs.stripe.com/invoicing/taxes/account-tax-ids
    https://docs.stripe.com/api/checkout/sessions/create#create_checkout_session-invoice_creation-invoice_data-account_tax_ids
    """
    params = {k: dict(v) for k, v in INVOICE_PARAMS.items()}
    txi = account_tax_id()
    if txi:
        params["invoice_creation"]["invoice_data"] = {"account_tax_ids": [txi]}
    return params


class SignatureError(ValueError):
    pass


class StripeError(RuntimeError):
    pass


def secret_key() -> str:
    return os.environ.get("STRIPE_SECRET_KEY", "")


def webhook_secret() -> str:
    return os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def configured() -> bool:
    return bool(secret_key() and webhook_secret())


def sign(payload: bytes, secret: str, ts: int) -> str:
    """Header-Wert so, wie Stripe ihn senden würde (für Tests/Fixtures)."""
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(
    payload: bytes, header: str, secret: str, *, tolerance: int = DEFAULT_TOLERANCE_S,
    now: float | None = None,
) -> dict:
    """Signatur prüfen und das Event als dict liefern; sonst SignatureError."""
    if not secret:
        raise SignatureError("no webhook secret configured")
    ts, sigs = None, []
    for part in (header or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v1" and v:
            sigs.append(v)
    if not ts or not sigs:
        raise SignatureError("malformed Stripe-Signature header")
    try:
        ts_i = int(ts)
    except ValueError as e:
        raise SignatureError("bad timestamp") from e
    expected = hmac.new(secret.encode(), f"{ts_i}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("signature mismatch")
    if abs((time.time() if now is None else now) - ts_i) > tolerance:
        raise SignatureError("timestamp outside tolerance")
    try:
        return json.loads(payload)
    except ValueError as e:
        raise SignatureError("payload is not JSON") from e


def _flatten(prefix: str, value, out: list[tuple[str, str]]) -> None:  # noqa: ANN001
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(f"{prefix}[{k}]" if prefix else k, v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    else:
        out.append((prefix, str(value)))


def form_encode(params: dict) -> bytes:
    """Stripe-Formkodierung für verschachtelte Parameter (a[b][0][c]=...)."""
    out: list[tuple[str, str]] = []
    _flatten("", params, out)
    return urllib.parse.urlencode(out).encode()


def create_checkout_session(params: dict, *, timeout: float = 10.0) -> dict:
    """Checkout-Session anlegen. Gibt das Stripe-Objekt (dict mit id, url) zurück."""
    key = secret_key()
    if not key:
        raise StripeError("STRIPE_SECRET_KEY not configured")
    req = urllib.request.Request(
        f"{STRIPE_API}/checkout/sessions",
        method="POST",
        data=form_encode(params),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            msg = ""
        raise StripeError(f"stripe {e.code}: {msg}") from e
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise StripeError(f"stripe unreachable: {e}") from e
