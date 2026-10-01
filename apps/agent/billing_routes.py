"""Aufladen + Wallet (portiert aus voicehook-v3 stripe_routes.py, Issue #53).

Ablauf (Oliver 01.10.2026):
  1. /aufladen: Betrag wählen -> POST /api/checkout -> Stripe Checkout (E-Mail dort).
  2. Stripe -> POST /api/stripe/webhook (Signatur geprüft) -> Guthaben auf ein NEUES
     Konto (oder auf das Konto des Wallet-Tokens, mit dem der Checkout gestartet
     wurde), idempotent je Session. Nie über die E-Mail zusammenführen: Stripe
     prüft die Adresse nicht (Review 01.10.: sonst Konto-Übernahme).
  3. Stripe leitet zurück auf /aufladen?session_id=... -> POST /api/wallet/claim
     -> Browser bekommt EINMAL ein geheimes Wallet-Token (localStorage) und einen
     Wiederherstellungs-Link (/aufladen#r=<code>).
  4. Calls: NUR host-call / live-room (wer den Raum anlegt) nehmen das Token im
     Header X-Wallet-Token an und binden den neuen Raum an das Konto; der Worker
     bucht dann echten Verbrauch x Faktor ab (billing/pricing.py). /api/token
     (Beitritt per Einladung) bindet nie: ein Gast zahlt nie für fremde Räume.
  5. Erstattung/Rückbuchung (charge.refunded, charge.dispute.created) ziehen den
     Betrag wieder ab (Saldo nie unter 0, Fehlbetrag vermerkt), idempotent.

Gating per Env, Default AUS bis Stripe live ist:
  VOICEHOOK_REQUIRE_CREDITS_NORMAL=0, VOICEHOOK_REQUIRE_CREDITS_LIVE=0
  (1 = ohne gültiges Wallet mit Saldo > 0 antwortet host-call/live-room mit 402).

Env: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, VOICEHOOK_PUBLIC_URL,
     VOICEHOOK_TOPUP_AMOUNTS_EUR ("10,20,50"), VOICEHOOK_TOPUP_MIN_EUR (10),
     VOICEHOOK_TOPUP_MAX_EUR (200), Preis-Env siehe billing/pricing.py.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import freetier
from .billing import db, pricing, stripe_api

logger = logging.getLogger("voicehook.billing")

router = APIRouter()

WALLET_HEADER = "x-wallet-token"
TOPUP_PATH = "/aufladen"
_OFF = {"", "0", "false", "off", "no", "aus"}


def _public_base() -> str:
    return os.environ.get("VOICEHOOK_PUBLIC_URL", "https://voicehook.ai").rstrip("/")


def credits_required(mode: str) -> bool:
    name = "VOICEHOOK_REQUIRE_CREDITS_LIVE" if mode == "live" else "VOICEHOOK_REQUIRE_CREDITS_NORMAL"
    return os.environ.get(name, "0").strip().lower() not in _OFF


def wallet_token(request: Request) -> str:
    return request.headers.get(WALLET_HEADER, "").strip()


def wallet_for_call(request: Request, mode: str) -> str | None:
    """Konto, das für einen neuen Call zahlt, oder None.

    Gating an + kein gültiges Token / Saldo <= 0 -> 402 mit Link zum Aufladen.
    Gating aus: ein Wallet mit Saldo > 0 wird trotzdem belastet (wer eins schickt,
    zahlt); ohne Wallet oder mit leerem Wallet läuft der Call wie bisher.
    """
    acc = db.account_for_token(wallet_token(request))
    if acc is not None and db.balance_ueur(acc) > 0:
        return acc
    if credits_required(mode):
        raise HTTPException(
            status_code=402,
            detail={"error": "credits_required", "topup_url": TOPUP_PATH,
                    "wallet": "empty" if acc else "missing"},
        )
    return None


def free_keys_for_call(request: Request, mode: str, ip: str, wallet: str | None) -> list[str] | None:
    """Gratis-Kontingent (freetier.py) für einen neuen Raum ohne Wallet.

    None = nicht zu zählen (bezahlter Raum oder Limit für den Modus aus).
    Sonst die Merkmale des Erstellers; ist eines davon heute schon am Limit -> 402
    mit Grund free_limit und Link zum Aufladen.
    """
    if wallet or not freetier.enabled(mode):
        return None
    keys = freetier.identity_keys(request.headers.get(freetier.ANON_HEADER), ip)
    if freetier.remaining_seconds(keys, mode) <= 0:
        raise HTTPException(
            status_code=402,
            detail={"error": "free_limit", "topup_url": TOPUP_PATH,
                    "free_min_per_day": freetier.limit_minutes(mode)},
        )
    return keys


# ----- Konfiguration für die Seite -------------------------------------------
@router.get("/api/billing/config")
def billing_config() -> dict:
    return {
        "currency": "EUR",
        "amounts_eur": pricing.topup_amounts_eur(),
        "min_eur": pricing.min_topup_eur(),
        "max_eur": pricing.max_topup_eur(),
        "checkout_available": stripe_api.configured(),
    }


# ----- Checkout ----------------------------------------------------------------
class CheckoutRequest(BaseModel):
    amount_eur: int = Field(..., ge=1, le=100_000)


@router.post("/api/checkout")
def api_checkout(req: CheckoutRequest, request: Request) -> dict:
    """Stripe-Checkout-Session für `amount_eur` (brutto, inkl. MwSt) anlegen."""
    if not stripe_api.configured():
        raise HTTPException(status_code=503, detail="top-up not available yet")
    lo, hi = pricing.min_topup_eur(), pricing.max_topup_eur()
    if not lo <= req.amount_eur <= hi:
        raise HTTPException(status_code=400, detail=f"amount must be between {lo} and {hi} EUR")
    base = _public_base()
    metadata = {"vh_amount_eur": str(req.amount_eur)}
    params: dict = {
        "mode": "payment",
        "locale": "auto",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "eur",
                "unit_amount": req.amount_eur * 100,
                "product_data": {"name": f"voicehook Guthaben {req.amount_eur} EUR"},
            },
        }],
        "success_url": f"{base}{TOPUP_PATH}?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}{TOPUP_PATH}?topup=cancel",
    }
    acc = db.account_for_token(wallet_token(request))
    if acc is not None:  # Aufladen aus bestehendem Wallet -> dasselbe Konto
        metadata["vh_account"] = acc
        row = db.account(acc)
        if row is not None:
            params["customer_email"] = row["email"]
    params["metadata"] = metadata
    try:
        session = stripe_api.create_checkout_session(params)
    except stripe_api.StripeError as e:
        logger.error("[billing] checkout create failed: %s", e)
        raise HTTPException(status_code=502, detail="payment provider error") from e
    logger.info("[billing] checkout %s %s EUR", session.get("id"), req.amount_eur)
    return {"url": session.get("url"), "session_id": session.get("id")}


# ----- Webhook -----------------------------------------------------------------
_PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
_REVERSAL_EVENTS = {"charge.refunded", "charge.dispute.created"}


def _id_of(v) -> str:  # noqa: ANN001  Stripe liefert IDs oder (expandiert) Objekte
    if isinstance(v, dict):
        v = v.get("id")
    return v if isinstance(v, str) else ""


def _handle_reversal(etype: str, obj: dict) -> dict:
    pi = _id_of(obj.get("payment_intent"))
    oid = obj.get("id") or ""
    if not pi or not oid:
        logger.error("[billing] %s without payment_intent/id, ignored", etype)
        return {"received": True, "ignored": "incomplete"}
    if etype == "charge.refunded":
        res = db.reverse_payment(pi, f"refund:{oid}:{int(obj.get('amount_refunded') or 0)}",
                                 "refund", int(obj.get("amount_refunded") or 0), cumulative=True)
    else:
        res = db.reverse_payment(pi, f"dispute:{oid}", "dispute", int(obj.get("amount") or 0))
    if res["status"] == "reversed" and res["shortfall_ueur"]:
        logger.warning("[billing] %s %s: balance short by %s µEUR (account %s), kept at 0",
                       etype, oid, res["shortfall_ueur"], res["account_id"])
    logger.info("[billing] %s %s -> %s", etype, oid, res["status"])
    return {"received": True, "reversal": res["status"]}


@router.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request) -> dict:
    """Gutschrift bei bezahlter Checkout-Session. Signatur Pflicht, idempotent je Session."""
    secret = stripe_api.webhook_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="webhook not configured")
    payload = await request.body()
    try:
        event = stripe_api.verify_signature(payload, request.headers.get("stripe-signature", ""), secret)
    except stripe_api.SignatureError as e:
        logger.warning("[billing] webhook rejected: %s", e)
        raise HTTPException(status_code=400, detail="invalid signature") from e

    etype = event.get("type", "")
    s = (event.get("data") or {}).get("object") or {}
    if etype in _REVERSAL_EVENTS:
        return _handle_reversal(etype, s)
    if etype not in _PAID_EVENTS:
        return {"received": True, "ignored": etype}
    sid = s.get("id")
    if s.get("payment_status") != "paid":  # z. B. SEPA: erst async_payment_succeeded
        return {"received": True, "ignored": "not_paid"}
    if (s.get("currency") or "").lower() != "eur":
        logger.error("[billing] session %s has currency %r, not crediting", sid, s.get("currency"))
        return {"received": True, "ignored": "currency"}
    amount = int(s.get("amount_total") or 0)
    email = (s.get("customer_details") or {}).get("email") or s.get("customer_email") or ""
    acc_hint = (s.get("metadata") or {}).get("vh_account")
    if not sid or amount <= 0 or not (email or acc_hint):
        logger.error("[billing] session %r incomplete (amount=%s email=%s)", sid, amount, bool(email))
        return {"received": True, "ignored": "incomplete"}
    granted = db.record_stripe_session(sid, email, amount, account_id=acc_hint,
                                       payment_intent=_id_of(s.get("payment_intent")) or None)
    logger.info("[billing] session %s %s (%s cents)", sid, "credited" if granted else "duplicate", amount)
    return {"received": True, "credited": granted, "duplicate": not granted}


# ----- Wallet ------------------------------------------------------------------
def _wallet_view(acc: str) -> dict:
    """Saldo-Ansicht. Bewusst OHNE E-Mail: das Konto ist das Wallet, nicht die Adresse."""
    row = db.account(acc)
    bal = int(row["balance_ueur"]) if row else 0
    return {
        "balance_eur": pricing.ueur_to_eur(bal),
        "currency": "EUR",
        "can_call": bal > 0,
    }


def _recovery_url(code: str) -> str:
    return f"{_public_base()}{TOPUP_PATH}#r={code}"


@router.get("/api/wallet")
def api_wallet(request: Request) -> dict:
    """Saldo. Nur mit gültigem Wallet-Token im Header X-Wallet-Token."""
    acc = db.account_for_token(wallet_token(request))
    if acc is None:
        raise HTTPException(status_code=401, detail="wallet token required")
    return _wallet_view(acc)


class ClaimRequest(BaseModel):
    session_id: str = Field(..., min_length=1, max_length=255)


@router.post("/api/wallet/claim")
def api_wallet_claim(req: ClaimRequest):  # noqa: ANN201
    """Nach Rückkehr von Stripe: Session einmalig gegen Wallet-Token + Recovery-Link tauschen."""
    state, acc = db.claim_session(req.session_id)
    if state == "pending":
        return JSONResponse(status_code=202, content={"pending": True})
    if state == "claimed" or acc is None:
        raise HTTPException(status_code=409, detail="already claimed")
    token = db.issue_token(acc, "wallet")
    code = db.issue_token(acc, "recovery")
    return {"wallet_token": token, "recovery_url": _recovery_url(code), **_wallet_view(acc)}


class RecoverRequest(BaseModel):
    code: str = Field(..., min_length=8, max_length=200)


@router.post("/api/wallet/recover")
def api_wallet_recover(req: RecoverRequest) -> dict:
    """Wiederherstellungs-Link einlösen -> neues Wallet-Token für dieses Gerät.

    Der Code wird dabei rotiert (Review 01.10. #7): der alte Link gilt nicht mehr,
    die Antwort enthält den neuen (recovery_url), die Seite zeigt ihn an."""
    got = db.redeem_recovery(req.code)
    if got is None:
        raise HTTPException(status_code=404, detail="unknown recovery code")
    acc, token, code = got
    return {"wallet_token": token, "recovery_url": _recovery_url(code), **_wallet_view(acc)}
