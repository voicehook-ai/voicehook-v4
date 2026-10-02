"""Checkout mit Rechnung: eigene USt-ID (Stripe-Tax-ID txi_...) explizit auf die Rechnung.

STRIPE_ACCOUNT_TAX_ID gesetzt + invoice=true -> der Body enthält
invoice_creation[invoice_data][account_tax_ids][0]=<txi>
(https://docs.stripe.com/invoicing/taxes/account-tax-ids). Env leer -> Parameter fehlt, sonst
alles wie bisher. invoice=false -> keinerlei invoice_creation-Parameter. Geprüft wird der echte
Formular-Body (urlopen gemockt, Stripe wird nie erreicht). Positivkontrolle: die Prüfung
unterscheidet Body mit und ohne Env (auf main ohne diese Änderung rot).
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from agent.billing import stripe_api
from agent.server import app

TXI = "txi_test_account_123"
KEY = "invoice_creation[invoice_data][account_tax_ids][0]"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_do_not_use")
    monkeypatch.setenv("VOICEHOOK_PUBLIC_URL", "https://vh.test")
    for k in ("VOICEHOOK_TOPUP_MIN_EUR", "VOICEHOOK_TOPUP_MAX_EUR", "STRIPE_ACCOUNT_TAX_ID"):
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
    box: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        box["body"] = req.data
        return _Resp()

    monkeypatch.setattr(stripe_api.urllib.request, "urlopen", fake_urlopen)
    return box


def _pairs(body: bytes) -> list[tuple[str, str]]:
    return urllib.parse.parse_qsl(body.decode())


def _tax_id_pairs(body: bytes) -> list[tuple[str, str]]:
    return [(k, v) for k, v in _pairs(body) if k.startswith("invoice_creation[invoice_data]")]


def _post(payload: dict) -> None:
    r = TestClient(app).post("/api/checkout", json=payload)
    assert r.status_code == 200, r.text


def test_env_and_invoice_sends_account_tax_id(sent, monkeypatch):
    monkeypatch.setenv("STRIPE_ACCOUNT_TAX_ID", TXI)
    _post({"amount_eur": 20, "invoice": True})
    assert _tax_id_pairs(sent["body"]) == [(KEY, TXI)]
    pairs = dict(_pairs(sent["body"]))
    # Rechnung + USt-ID-Erfassung des Kunden bleiben wie in #133
    assert pairs["invoice_creation[enabled]"] == "true"
    assert pairs["tax_id_collection[enabled]"] == "true"
    assert pairs["line_items[0][price_data][unit_amount]"] == "2000"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_without_env_no_account_tax_id(sent, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("STRIPE_ACCOUNT_TAX_ID", value)
    _post({"amount_eur": 20, "invoice": True})
    assert _tax_id_pairs(sent["body"]) == []
    assert b"account_tax_ids" not in sent["body"] and b"invoice_data" not in sent["body"]
    # altes Verhalten: Rechnung trotzdem an
    assert dict(_pairs(sent["body"]))["invoice_creation[enabled]"] == "true"


@pytest.mark.parametrize("payload", [{"amount_eur": 20}, {"amount_eur": 20, "invoice": False}])
def test_no_invoice_never_sends_account_tax_id(sent, monkeypatch, payload):
    monkeypatch.setenv("STRIPE_ACCOUNT_TAX_ID", TXI)
    _post(payload)
    assert b"account_tax_ids" not in sent["body"] and b"invoice_creation" not in sent["body"]
    assert TXI.encode() not in sent["body"]


def test_positive_control_tells_bodies_apart(sent, monkeypatch):
    """Die Prüfung schlägt an: Body mit Env != Body ohne Env, Differenz genau der eine Parameter."""
    _post({"amount_eur": 20, "invoice": True})
    without = sent["body"]
    monkeypatch.setenv("STRIPE_ACCOUNT_TAX_ID", TXI)
    _post({"amount_eur": 20, "invoice": True})
    with_txi = sent["body"]
    assert with_txi != without
    assert _tax_id_pairs(with_txi) and not _tax_id_pairs(without)
    stripped = urllib.parse.urlencode(
        [(k, v) for k, v in _pairs(with_txi) if not k.startswith("invoice_creation[invoice_data]")]
    ).encode()
    assert stripped == without


def test_invoice_params_does_not_mutate_constant(monkeypatch):
    monkeypatch.setenv("STRIPE_ACCOUNT_TAX_ID", TXI)
    stripe_api.invoice_params()
    assert stripe_api.INVOICE_PARAMS["invoice_creation"] == {"enabled": "true"}
