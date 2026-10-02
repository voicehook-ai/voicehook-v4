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

Reihenfolge im Call: erst Gratis-Euro (freetier.py), dann Guthaben
(payer_for_call). Gating per Env greift nur, wenn das Gratis-Kontingent des Modus
aus ist: VOICEHOOK_REQUIRE_CREDITS_NORMAL=0, VOICEHOOK_REQUIRE_CREDITS_LIVE=0
  (1 = ohne gültiges Wallet mit Saldo > 0 antwortet host-call/live-room mit 402).

Login (Magic-Link, Oliver 01.10.): POST /api/login {email} schickt einen Link an
die Adresse; POST /api/login/verify {token, nonce} tauscht ihn einmalig (15 min)
gegen ein Wallet-Token für das Konto mit dieser BESTÄTIGTEN Adresse. Ohne die
login_nonce des anfordernden Browsers nur nach Bestätigung (confirm). Die
Stripe-Mail allein verknüpft nie (siehe billing/db.py login_verified_email).
Eingelöst wird NUR per POST nach Klick auf "Jetzt anmelden" (Oliver 02.10.):
Mail-Scanner (Safe Links, Prefetch) rufen Links per GET auf und dürfen den
einmaligen Token nie verbrauchen. GET /api/login/verify verbraucht nichts und
leitet auf die Bestätigungsseite um.

Env: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, VOICEHOOK_PUBLIC_URL,
     VOICEHOOK_TOPUP_AMOUNTS_EUR ("5,10,20,50"), VOICEHOOK_TOPUP_MIN_EUR (5),
     VOICEHOOK_TOPUP_MAX_EUR (200), Preis-Env siehe billing/pricing.py.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import freetier
from .billing import db, mail, pricing, stripe_api

logger = logging.getLogger("voicehook.billing")

router = APIRouter()

WALLET_HEADER = "x-wallet-token"
TOPUP_PATH = "/aufladen"
_OFF = {"", "0", "false", "off", "no", "aus"}


def _public_base() -> str:
    return os.environ.get("VOICEHOOK_PUBLIC_URL", "https://voicehook.ai").rstrip("/")


def client_ip(request: Request) -> str:
    """Client-IP hinter Caddy. Das LETZTE X-Forwarded-For-Element hat der nächste
    Proxy (Caddy) selbst gesetzt; frühere Elemente kann der Client fälschen.
    Caddy ohne trusted_proxies (infra/caddy/Caddyfile.tmpl) verwirft eingehende
    X-Forwarded-For-Werte ohnehin, das letzte Element bleibt aber auch dann richtig,
    wenn dort später trusted_proxies gesetzt wird (Review 01.10. #4)."""
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def credits_required(mode: str) -> bool:
    name = "VOICEHOOK_REQUIRE_CREDITS_LIVE" if mode == "live" else "VOICEHOOK_REQUIRE_CREDITS_NORMAL"
    return os.environ.get(name, "0").strip().lower() not in _OFF


def wallet_token(request: Request) -> str:
    return request.headers.get(WALLET_HEADER, "").strip()


def request_account(request: Request) -> str | None:
    """Konto zum X-Wallet-Token des Browsers (oder None). Ob es als angemeldet zählt,
    entscheidet freetier.account_exempt (nur bestätigte Mail)."""
    return db.account_for_token(wallet_token(request))


def payer_for_call(request: Request, mode: str, ip: str) -> tuple[str | None, list[str] | None]:
    """Wer zahlt einen NEUEN Raum: (Wallet-Konto | None, Gratis-Merkmale | None).

    Reihenfolge (Oliver 01.10.): erst der Gratis-Euro des Tages, dann das Guthaben.
    Hat der Anfragende heute noch Gratis-Rest, kommen seine Merkmale zurück (der
    Worker zählt sie zuerst herunter); hat er zusätzlich ein Wallet mit Saldo > 0,
    wird der Raum auch daran gebunden, der Worker bucht nach dem Gratis-Teil vom
    Guthaben weiter. Weder Gratis-Rest noch Guthaben -> 402 mit Link zum Aufladen:
      free_limit        Gratis-Kontingent an, heute aufgebraucht (Normal + Live gemeinsam)
      credits_required  Gratis-Kontingent für den Modus aus und
                        VOICEHOOK_REQUIRE_CREDITS_<MODE>=1
    Gratis aus und Gating aus: (None, None), der Call läuft wie bisher ungezählt.
    """
    acc = db.account_for_token(wallet_token(request))
    wallet = acc if acc is not None and db.balance_ueur(acc) > 0 else None
    if freetier.enabled(mode):
        keys = freetier.identity_keys(request.headers.get(freetier.ANON_HEADER), ip)
        st = freetier.free_state(keys, account=acc)  # dieselbe Rechnung wie /api/me und Worker
        if st["exempt"] or st["left_ueur"] > 0:
            return wallet, keys
        if wallet:
            return wallet, None
        raise HTTPException(
            status_code=402,
            detail={"error": "free_limit", "topup_url": TOPUP_PATH,
                    "free_eur_per_day": st["eur_per_day"]},
        )
    if wallet:
        return wallet, None
    if credits_required(mode):
        raise HTTPException(
            status_code=402,
            detail={"error": "credits_required", "topup_url": TOPUP_PATH,
                    "wallet": "empty" if acc else "missing"},
        )
    return None, None


def register_new_room(room: str, mode: str, wallet: str | None, free_keys: list[str] | None,
                      ttl_seconds: int, *, account: str | None = None) -> None:
    """Zuordnungen für den Worker anlegen, VOR dem Dispatch (er liest sie beim Start).
    `account` = Konto des Anfragenden (X-Wallet-Token), damit der Worker die Konto-
    Ausnahme (VH_FREE_EXEMPT_ACCOUNTS) mit derselben Funktion prüft wie der Server."""
    if wallet:
        db.bind_room(room, wallet, mode, ttl_seconds)
    if free_keys is not None:
        freetier.register_room(room, mode, free_keys, account=account)


# ----- Konfiguration für die Seite -------------------------------------------
# Ungefährer Kundenpreis pro Gesprächsstunde (inkl. Marge und MwSt), nur zur
# Anzeige am Normal/Live-Schalter ("ca."). Abgebucht wird immer der echte
# Verbrauch (billing/pricing.py). Defaults aus Messung (Oliver 01.10.2026).
DEFAULT_APPROX_EUR_PER_HOUR = {"normal": 1.70, "live": 8.80}


def approx_eur_per_hour(mode: str) -> float:
    """Env VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL / _LIVE; kaputt oder < 0 -> Default."""
    default = DEFAULT_APPROX_EUR_PER_HOUR["live" if mode == "live" else "normal"]
    name = "VOICEHOOK_APPROX_EUR_PER_HOUR_LIVE" if mode == "live" else "VOICEHOOK_APPROX_EUR_PER_HOUR_NORMAL"
    raw = os.environ.get(name, "").strip().replace(",", ".")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        return default
    return round(v, 2) if math.isfinite(v) and v >= 0 else default


@router.get("/api/billing/config")
def billing_config() -> dict:
    return {
        "currency": "EUR",
        "amounts_eur": pricing.topup_amounts_eur(),
        "min_eur": pricing.min_topup_eur(),
        "max_eur": pricing.max_topup_eur(),
        "checkout_available": stripe_api.configured(),
        "approx_eur_per_hour": {m: approx_eur_per_hour(m) for m in ("normal", "live")},
    }


# ----- Checkout ----------------------------------------------------------------
class CheckoutRequest(BaseModel):
    # float, damit 4,99 als 400 (unter Minimum) ankommt statt als 422; nur ganze Euro.
    amount_eur: float = Field(..., gt=0, le=100_000)
    # Rechnung mit USt-ID nur auf Wunsch (Checkbox auf /aufladen): Stripe berechnet
    # jede erzeugte Rechnung extra, deshalb Standard aus.
    invoice: bool = False


@router.post("/api/checkout")
def api_checkout(req: CheckoutRequest, request: Request) -> dict:
    """Stripe-Checkout-Session für `amount_eur` (brutto, inkl. MwSt) anlegen."""
    if not stripe_api.configured():
        raise HTTPException(status_code=503, detail="top-up not available yet")
    lo, hi = pricing.min_topup_eur(), pricing.max_topup_eur()
    if req.amount_eur != int(req.amount_eur) or not lo <= req.amount_eur <= hi:
        raise HTTPException(status_code=400, detail=f"amount must be a whole number between {lo} and {hi} EUR")
    amount_eur = int(req.amount_eur)
    base = _public_base()
    metadata = {"vh_amount_eur": str(amount_eur)}
    params: dict = {
        "mode": "payment",
        "locale": "auto",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": "eur",
                "unit_amount": amount_eur * 100,
                "product_data": {"name": f"voicehook Guthaben {amount_eur} EUR"},
            },
        }],
        "success_url": f"{base}{TOPUP_PATH}?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}{TOPUP_PATH}?topup=cancel",
    }
    if req.invoice:
        params.update(stripe_api.invoice_params())
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
    logger.info("[billing] checkout %s %s EUR", session.get("id"), amount_eur)
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


def mask_email(email: str) -> str | None:
    """o***@n***.ai: genug zum Wiedererkennen, nicht genug zum Abschreiben."""
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return None
    host, dot, tld = domain.rpartition(".")
    if not host:
        host, dot, tld = domain, "", ""
    return f"{local[0]}***@{host[0]}***{dot}{tld}"


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


@router.post("/api/logout")
def api_logout(request: Request) -> dict:
    """Abmelden auf diesem Gerät: widerruft genau das Token aus X-Wallet-Token.
    Immer 200 (ohne/unbekanntes Token: no-op, revoked false). Andere Geräte
    desselben Kontos bleiben angemeldet."""
    revoked = db.revoke_wallet_token(wallet_token(request) or None)
    if revoked:
        logger.info("[logout] wallet token revoked")
    return {"ok": True, "revoked": revoked}


# ----- Magic-Link-Login -----------------------------------------------------------
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
# Ratenlimits Magic-Link (Re-Review PR #93):
#   je IP-Bucket (IPv4 voll, IPv6 /64)      5 in 10 min
#   je Adresse + IP-Bucket                  3 in 15 min
#   je Adresse (alle IPs zusammen)         10 in 60 min
# Ein Angreifer aus fremden Netzen sperrt damit die Adresse höchstens eine Stunde,
# nicht mit 3 Anfragen; das Opfer aus seinem eigenen Netz kommt bis dahin durch.
LOGIN_IP_LIMIT, LOGIN_IP_WINDOW = 5, 600
LOGIN_MAIL_IP_LIMIT, LOGIN_MAIL_IP_WINDOW = 3, 900
LOGIN_MAIL_LIMIT, LOGIN_MAIL_WINDOW = 10, 3600
LOGIN_HITS_MAX = 10_000
# LRU statt clear(): wer die Tabelle mit neuen Schlüsseln flutet, verdrängt nur die
# am längsten unbenutzten, setzt aber nicht alle Limits auf einmal zurück.
_LOGIN_HITS: OrderedDict[str, list[float]] = OrderedDict()
_LOGIN_LOCK = threading.Lock()


def _login_rate_take(rules: list[tuple[str, int, float]]) -> bool:
    """Alle Limits (key, limit, window) prüfen; nur wenn ALLE frei sind, zählt die
    Anfrage bei allen (atomar). Eine abgelehnte Anfrage verbraucht nichts."""
    now = time.time()
    with _LOGIN_LOCK:
        fresh = {}
        for key, limit, window in rules:
            hits = [t for t in _LOGIN_HITS.get(key, []) if now - t < window]
            fresh[key] = hits
            if len(hits) >= limit:
                ok = False
                break
        else:
            ok = True
        for key, hits in fresh.items():
            if ok:
                hits.append(now)
            _LOGIN_HITS[key] = hits
            _LOGIN_HITS.move_to_end(key)
        while len(_LOGIN_HITS) > LOGIN_HITS_MAX:
            _LOGIN_HITS.popitem(last=False)
        return ok


def _login_rate_give_back(rules: list[tuple[str, int, float]]) -> None:
    """Anfrage zählt doch nicht (Mail-Versand fehlgeschlagen): je Schlüssel den
    jüngsten Eintrag wieder entfernen."""
    with _LOGIN_LOCK:
        for key, _limit, _window in rules:
            hits = _LOGIN_HITS.get(key)
            if hits:
                hits.pop()


def _login_rules(email: str, ip: str) -> list[tuple[str, int, float]]:
    bucket = freetier.ip_bucket(ip)
    mail_h = hashlib.sha256(email.encode()).hexdigest()
    mail_ip_h = hashlib.sha256(f"{email}|{bucket}".encode()).hexdigest()
    return [
        ("ip:" + bucket, LOGIN_IP_LIMIT, LOGIN_IP_WINDOW),
        ("mailip:" + mail_ip_h, LOGIN_MAIL_IP_LIMIT, LOGIN_MAIL_IP_WINDOW),
        ("mail:" + mail_h, LOGIN_MAIL_LIMIT, LOGIN_MAIL_WINDOW),
    ]


def login_link(token: str) -> str:
    """Link in der Mail. Token im Fragment: Mail-Scanner, die Links vorab abrufen,
    sehen es nie; die Seite liest es, zeigt "Jetzt anmelden" und löst es erst nach
    dem Klick per POST /api/login/verify ein."""
    return f"{_public_base()}{TOPUP_PATH}#login={token}"


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


@router.post("/api/login")
def api_login(req: LoginRequest, request: Request) -> dict:
    """Login-Link an `email` schicken. Antwort immer gleich, egal ob es zu der
    Adresse ein Konto gibt (sonst ließe sich abfragen, wer Kunde ist)."""
    if not mail.configured():
        raise HTTPException(status_code=503, detail="login_unavailable")
    email = db.normalize_email(req.email)
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="invalid_email")
    rules = _login_rules(email, client_ip(request))
    if not _login_rate_take(rules):
        raise HTTPException(status_code=429, detail="rate_limited")
    # Wallet des anfordernden Browsers an den Link binden (Login-CSRF, PR #93):
    # nur genau dieses Wallet darf beim Einlösen verknüpft werden. Die Nonce bekommt
    # nur dieser Browser; ohne sie verlangt verify eine Bestätigung (Rest-CSRF).
    token, nonce = db.create_login_link(email, requester_token=wallet_token(request) or None)
    try:
        mail.send(email, mail.login_message(login_link(token), db.LOGIN_TTL_S // 60))
    except mail.MailError as e:
        _login_rate_give_back(rules)  # Fehlversuch zählt nicht gegen die Limits
        logger.error("[login] mail send failed: %s", e)
        raise HTTPException(status_code=502, detail="mail_failed") from e
    logger.info("[login] link sent")
    return {"sent": True, "expires_in": db.LOGIN_TTL_S, "login_nonce": nonce}


class LoginVerifyRequest(BaseModel):
    """Body von POST /api/login/verify. Token nie in der URL (History, Referer, Logs)."""
    token: str = Field(..., min_length=1, max_length=1000)
    nonce: str | None = Field(None, max_length=1000)
    confirm: bool = False


LOGIN_CONFIRM_PATH = "/login"


@router.get("/api/login/verify")
def api_login_verify_get(token: str | None = None) -> RedirectResponse:
    """Alte bzw. direkt aufgerufene GET-Links: verbrauchen NIE etwas (Mail-Scanner,
    Prefetch). 303 auf die Bestätigungsseite mit dem Token im Fragment; dort löst
    erst der Klick auf "Jetzt anmelden" per POST ein. Ohne Token nur /login."""
    loc = LOGIN_CONFIRM_PATH
    if token and len(token) <= 200:
        loc += "#login=" + urllib.parse.quote(token, safe="")
    return RedirectResponse(loc, status_code=303, headers={
        "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


@router.post("/api/login/verify")
def api_login_verify(req: LoginVerifyRequest, request: Request):  # noqa: ANN201
    """Magic-Link einlösen (einmal, 15 min) -> Wallet-Token für das Konto mit dieser
    jetzt bestätigten Adresse. Nur per POST (Klick auf "Jetzt anmelden").

    nonce = login_nonce aus der Antwort von POST /api/login. Fehlt sie oder passt
    sie nicht (Link in einem anderen Browser geöffnet, z. B. ein vom Angreifer an
    SEINE Adresse angeforderter Link), wird der Link NICHT verbraucht: 409
    {"error": "confirm_required", "email_masked": ...}; die Seite fragt "Anmelden
    als <maske>?" und schickt bei Ja erneut mit confirm=true (Login auf anderem
    Gerät). Dann wird eingeloggt, ein Wallet dieses Browsers aber nie verknüpft.

    Mit passender Nonce zählt X-Wallet-Token nur, wenn es exakt das Token ist, mit
    dem der Link angefordert wurde (Login-CSRF, PR #93): dann wird dieses Wallet
    bestätigt bzw. in das Konto überführt (wallet_linked true), sonst bleibt es
    unberührt (wallet_linked false). Regeln in billing/db.py login_verified_email."""
    link = db.consume_login_link(req.token, nonce=req.nonce, confirm=req.confirm)
    if link is None:
        raise HTTPException(status_code=400, detail="invalid_or_expired")
    if link.status == "confirm_required":
        return JSONResponse(status_code=409, content={
            "error": "confirm_required", "email_masked": mask_email(link.email)})
    acc, wallet, code, linked = db.login_verified_email(
        link.email, wallet_token=wallet_token(request) or None, requester_hash=link.requester_hash,
    )
    logger.info("[login] verified account %s (wallet_linked=%s)", acc, linked)
    return {"wallet_token": wallet, "recovery_url": _recovery_url(code), "email_verified": True,
            "wallet_linked": linked, "email_masked": mask_email(link.email), **_wallet_view(acc)}
