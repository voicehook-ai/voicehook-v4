"""Checkout: Rechnung (invoice_creation) + USt-ID-Erfassung (tax_id_collection).

Nur auf Wunsch (invoice=true, Checkbox auf /aufladen), weil Stripe jede Rechnung
extra berechnet. Ohne invoice bzw. invoice=false enthält der Body keinen der beiden
Parameter. Geprüft wird der echte Formular-Body, den create_checkout_session an Stripe
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


def _pairs(body: bytes) -> dict:
    return dict(urllib.parse.parse_qsl(body.decode()))


def _assert_flow_unchanged(body: bytes) -> None:
    # Bestehender Fluss unverändert: Betrag, Modus, Metadaten für die Webhook-Gutschrift
    pairs = _pairs(body)
    assert pairs["mode"] == "payment"
    assert pairs["line_items[0][price_data][unit_amount]"] == "2000"
    assert pairs["metadata[vh_amount_eur]"] == "20"
    # Nur die zwei Schalter, keine weiteren Kunden-/Adress-Parameter
    assert not any(k.startswith(("customer_creation", "billing_address_collection")) for k in pairs)


def _has_no_invoice_params(body: bytes) -> bool:
    return not any(k.startswith(("invoice_creation", "tax_id_collection")) for k in _pairs(body))


def test_checkout_invoice_true_enables_invoice_and_tax_id(sent):
    r = TestClient(app).post("/api/checkout", json={"amount_eur": 20, "invoice": True})
    assert r.status_code == 200
    assert sent["url"] == "https://api.stripe.com/v1/checkout/sessions"
    assert _has_invoice_params(sent["body"])
    _assert_flow_unchanged(sent["body"])


@pytest.mark.parametrize("payload", [{"amount_eur": 20}, {"amount_eur": 20, "invoice": False}])
def test_checkout_without_invoice_sends_neither_param(sent, payload):
    r = TestClient(app).post("/api/checkout", json=payload)
    assert r.status_code == 200
    assert _has_no_invoice_params(sent["body"])
    assert b"invoice_creation" not in sent["body"] and b"tax_id_collection" not in sent["body"]
    _assert_flow_unchanged(sent["body"])


def test_positive_control_checks_tell_bodies_apart(sent):
    """Beide Prüfungen schlagen auf dem jeweils anderen Body an (nicht immer grün)."""
    client = TestClient(app)
    client.post("/api/checkout", json={"amount_eur": 20, "invoice": True})
    with_inv = sent["body"]
    client.post("/api/checkout", json={"amount_eur": 20})
    without = sent["body"]
    assert with_inv != without
    assert _has_invoice_params(with_inv) and not _has_no_invoice_params(with_inv)
    assert _has_no_invoice_params(without) and not _has_invoice_params(without)
    # Gefilterter invoice-Body == Body ohne invoice: sonst unterscheidet sich nichts.
    stripped = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(with_inv.decode())
         if not k.startswith(("invoice_creation", "tax_id_collection"))]
    ).encode()
    assert stripped == without
