"""Anmelden mit Google / GitHub (OAuth 2.0 Authorization Code + PKCE S256).

Eigene, schlanke Implementierung (urllib wie billing/stripe_api.py, keine neue
Abhängigkeit). Der Anbieter liefert nur eine BESTÄTIGTE E-Mail; angemeldet wird
danach exakt wie per Magic-Link (billing_routes.py, PR #93):

  1. /login: POST /api/auth/<p>/start {next} (mit X-Wallet-Token, falls vorhanden)
     -> {authorize_url, login_nonce}. Der Server legt einen einmaligen state
     (10 min) mit PKCE-Verifier, next, Nonce-Hash und Wallet-Hash an. Die Seite
     legt login_nonce in localStorage vh_login_nonce (wie beim Mail-Link) und
     springt zu authorize_url.
  2. Anbieter -> GET /api/auth/<p>/callback?code&state: state prüfen und
     verbrauchen, Code tauschen (mit code_verifier), verifizierte E-Mail holen
     (Google: userinfo email_verified=true; GitHub: /user/emails primary+verified).
  3. Für diese E-Mail entsteht ein normaler Login-Link (db.create_login_link_for_nonce),
     gebunden an die Nonce und das Wallet aus Schritt 1, und der Browser geht per
     302 auf /login?next=<next>#login=<token>. /login zeigt "Jetzt anmelden" und löst
     ihn erst nach dem Klick über POST /api/login/verify {token, nonce} ein: passende
     Nonce -> 200, sonst 409 confirm_required mit Rückfrage. Ein Angreifer, der dem Opfer SEINEN Callback-
     oder #login=-Link schickt, schaltet dessen Browser also nie still um.

Fehler im Callback -> 302 /login?next=<next>#error=<code>
  (denied | state | provider | email_unverified | unavailable).

Env: GOOGLE_OAUTH_CLIENT_ID + GOOGLE_OAUTH_CLIENT_SECRET,
     GITHUB_OAUTH_CLIENT_ID + GITHUB_OAUTH_CLIENT_SECRET (fehlt eins -> Anbieter
     aus, Endpunkte 503), Callback-Basis VOICEHOOK_PUBLIC_URL.
Logs: nie Code, Token, state oder Secret; nur Anbieter und Ergebnis.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import freetier
from .billing import db, mail
from .billing_routes import (
    LOGIN_IP_LIMIT,
    LOGIN_IP_WINDOW,
    _login_rate_take,
    _public_base,
    client_ip,
    wallet_token,
)

logger = logging.getLogger("voicehook.oauth")

router = APIRouter()

LOGIN_PATH = "/login"
DEFAULT_NEXT = "/aufladen"
NEXT_MAX = 512

PROVIDERS: dict[str, dict] = {
    "google": {
        "env": "GOOGLE_OAUTH",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email",
    },
    "github": {
        "env": "GITHUB_OAUTH",
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "user": "https://api.github.com/user",
        "emails": "https://api.github.com/user/emails",
        "scope": "user:email",
    },
}


class ProviderError(RuntimeError):
    """Anbieter nicht erreichbar oder Antwort unbrauchbar (nie mit Secrets im Text)."""


# ----- Konfiguration ------------------------------------------------------------
def _creds(provider: str) -> tuple[str, str]:
    env = PROVIDERS[provider]["env"]
    return (os.environ.get(f"{env}_CLIENT_ID", "").strip(),
            os.environ.get(f"{env}_CLIENT_SECRET", "").strip())


def configured(provider: str) -> bool:
    cid, secret = _creds(provider)
    return bool(cid and secret)


def redirect_uri(provider: str) -> str:
    return f"{_public_base()}/api/auth/{provider}/callback"


def safe_next(value: str | None) -> str:
    """Nur relative Pfade auf dieser Seite (Open-Redirect): beginnt mit genau einem
    "/", kein "//" oder "/\\" (Browser lesen beides als fremden Host), keine
    Steuerzeichen, nicht /login selbst. Alles andere -> DEFAULT_NEXT."""
    v = value if isinstance(value, str) else ""
    if (not v or len(v) > NEXT_MAX or not v.startswith("/") or v.startswith("//")
            or "\\" in v or any(ord(c) < 0x20 or ord(c) == 0x7F for c in v)):
        return DEFAULT_NEXT
    path = v.split("?", 1)[0].split("#", 1)[0]
    if path == LOGIN_PATH or path.startswith(LOGIN_PATH + "/") or path == LOGIN_PATH + ".html":
        return DEFAULT_NEXT
    return v


def _login_url(next_path: str, fragment: str) -> str:
    q = "" if next_path == DEFAULT_NEXT else "?next=" + urllib.parse.quote(next_path, safe="/")
    return f"{LOGIN_PATH}{q}#{fragment}"


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ----- HTTP zum Anbieter (in Tests gemockt) -------------------------------------
def _http_json(url: str, *, data: dict | None = None, headers: dict | None = None,
               timeout: float = 10.0):  # noqa: ANN202
    h = {"Accept": "application/json", "User-Agent": "voicehook-login"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"http {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise ProviderError(type(e).__name__) from None


def _exchange_code(provider: str, code: str, verifier: str) -> str:
    cid, secret = _creds(provider)
    res = _http_json(PROVIDERS[provider]["token"], data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider),
        "client_id": cid, "client_secret": secret, "code_verifier": verifier,
    })
    tok = res.get("access_token") if isinstance(res, dict) else None
    if not isinstance(tok, str) or not tok:
        # GitHub antwortet auch bei Fehlern mit 200 {"error": ...}
        raise ProviderError("no access_token")
    return tok


def _google_identity(access_token: str) -> tuple[str, str] | None:
    """(sub, email) nur bei email_verified=true, sonst None."""
    info = _http_json(PROVIDERS["google"]["userinfo"],
                      headers={"Authorization": f"Bearer {access_token}"})
    if not isinstance(info, dict):
        raise ProviderError("bad userinfo")
    verified = info.get("email_verified")
    email, sub = info.get("email"), info.get("sub")
    if verified is not True and verified != "true":
        return None
    if not isinstance(email, str) or "@" not in email or not sub:
        return None
    return str(sub), email


def _github_identity(access_token: str) -> tuple[str, str] | None:
    """(id, email) mit der Adresse, die bei GitHub primary UND verified ist."""
    h = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    user = _http_json(PROVIDERS["github"]["user"], headers=h)
    emails = _http_json(PROVIDERS["github"]["emails"], headers=h)
    if not isinstance(user, dict) or not user.get("id") or not isinstance(emails, list):
        raise ProviderError("bad user/emails")
    for e in emails:
        if (isinstance(e, dict) and e.get("primary") is True and e.get("verified") is True
                and isinstance(e.get("email"), str) and "@" in e["email"]):
            return str(user["id"]), e["email"]
    return None


_IDENTITY = {"google": _google_identity, "github": _github_identity}


# ----- Endpunkte ----------------------------------------------------------------
@router.get("/api/auth/providers")
def api_auth_providers() -> dict:
    """Welche Anmeldewege serverseitig eingerichtet sind (Seite /login graut den Rest aus)."""
    return {"google": configured("google"), "github": configured("github"),
            "email": mail.configured()}


def _provider_or_404(provider: str) -> str:
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown_provider")
    return provider


class StartRequest(BaseModel):
    next: str | None = Field(None, max_length=2048)


@router.post("/api/auth/{provider}/start")
def api_auth_start(provider: str, request: Request, req: StartRequest | None = None) -> dict:
    provider = _provider_or_404(provider)
    if not configured(provider):
        raise HTTPException(status_code=503, detail="provider_unavailable")
    bucket = freetier.ip_bucket(client_ip(request))
    if not _login_rate_take([("oauthip:" + bucket, LOGIN_IP_LIMIT, LOGIN_IP_WINDOW)]):
        raise HTTPException(status_code=429, detail="rate_limited")
    next_path = safe_next(req.next if req else None)
    state, nonce, verifier = db.create_oauth_state(
        provider, next_path, requester_token=wallet_token(request) or None)
    p = PROVIDERS[provider]
    params = {
        "client_id": _creds(provider)[0], "redirect_uri": redirect_uri(provider),
        "response_type": "code", "scope": p["scope"], "state": state,
        "code_challenge": _pkce_challenge(verifier), "code_challenge_method": "S256",
    }
    logger.info("[oauth] %s start", provider)
    return {"authorize_url": f"{p['authorize']}?{urllib.parse.urlencode(params)}",
            "login_nonce": nonce, "expires_in": db.OAUTH_STATE_TTL_S}


def _fail(next_path: str, code: str, provider: str) -> RedirectResponse:
    logger.info("[oauth] %s callback failed: %s", provider, code)
    return RedirectResponse(_login_url(next_path, "error=" + code), status_code=302)


@router.get("/api/auth/{provider}/callback")
def api_auth_callback(provider: str, code: str | None = None, state: str | None = None,
                      error: str | None = None) -> RedirectResponse:
    provider = _provider_or_404(provider)
    if not configured(provider):
        raise HTTPException(status_code=503, detail="provider_unavailable")
    st = db.consume_oauth_state(provider, state)  # immer verbrauchen, auch bei error=
    if st is None:
        return _fail(DEFAULT_NEXT, "state", provider)
    if error or not code or len(code) > 2048:
        return _fail(st.next_path, "denied" if error else "provider", provider)
    try:
        access = _exchange_code(provider, code, st.code_verifier)
        ident = _IDENTITY[provider](access)
    except ProviderError as e:
        logger.warning("[oauth] %s provider error: %s", provider, e)
        return _fail(st.next_path, "provider", provider)
    if ident is None:
        return _fail(st.next_path, "email_unverified", provider)
    subject, email = ident
    db.record_oauth_identity(provider, subject, email)
    token = db.create_login_link_for_nonce(email, nonce_hash=st.nonce_hash,
                                           requester_hash=st.requester_hash)
    logger.info("[oauth] %s verified, login link issued", provider)
    return RedirectResponse(_login_url(st.next_path, "login=" + token), status_code=302)
