"""Checkout: Rechnung (invoice_creation) + USt-ID-Erfassung (tax_id_collection).

Geprüft wird der echte Formular-Body, den create_checkout_session an Stripe
schicken würde (urlopen gemockt, Stripe wird nie erreicht). Positivkontrolle:
derselbe Body ohne die neuen Parameter fällt durch dieselbe Prüfung.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from agent.billing import stripe_api
from agent.server import app

WANTED = {"invoice_creation[enabled]": "true", "tax_id_collection[enabled]": "true"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_do_not_use")
    monkeypatch.setenv("VOICEHOOK_PUBLIC_URL", "https://vh.test")
    for k in ("VOICEHOOK_TOPUP_MIN_EUR", "VOICEHOOK_TOPUP_MAX_EUR"):
        monkeypatch.delenv(k, raising=False)


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"id": "cs_body", "url": "https://checkout.stripe.test/cs_body"}).encode()


@pytest.fixture
def sent(monkeypatch):
    """Fängt den Request ab, den stripe_api an api.stripe.com schicken würde."""
    box: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        box["url"], box["body"] = req.full_url, req.data
        return _Resp()

    monkeypatch.setattr(stripe_api.urllib.request, "urlopen", fake_urlopen)
    return box


def _has_invoice_params(body: bytes) -> bool:
    pairs = dict(urllib.parse.parse_qsl(body.decode()))
    return all(pairs.get(k) == v for k, v in WANTED.items())


def test_checkout_body_enables_invoice_and_tax_id(sent):
    r = TestClient(app).post("/api/checkout", json={"amount_eur": 20})
    assert r.status_code == 200
    assert sent["url"] == "https://api.stripe.com/v1/checkout/sessions"
    assert _has_invoice_params(sent["body"])
    # Bestehender Fluss unverändert: Betrag, Modus, Metadaten für die Webhook-Gutschrift
    pairs = dict(urllib.parse.parse_qsl(sent["body"].decode()))
    assert pairs["mode"] == "payment"
    assert pairs["line_items[0][price_data][unit_amount]"] == "2000"
    assert pairs["metadata[vh_amount_eur]"] == "20"
    # Nur die zwei Schalter, keine weiteren Kunden-/Adress-Parameter
    assert not any(k.startswith(("customer_creation", "billing_address_collection")) for k in pairs)


def test_positive_control_body_without_change_fails_check(sent):
    TestClient(app).post("/api/checkout", json={"amount_eur": 20})
    pairs = urllib.parse.parse_qsl(sent["body"].decode())
    old = urllib.parse.urlencode(
        [(k, v) for k, v in pairs if not k.startswith(("invoice_creation", "tax_id_collection"))]
    ).encode()
    assert old != sent["body"]
    assert not _has_invoice_params(old)          # Prüfung erkennt das Fehlen
    assert b"invoice_creation" not in old and b"tax_id_collection" not in old
