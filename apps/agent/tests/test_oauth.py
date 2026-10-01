"""Anmelden mit Google/GitHub (oauth_routes.py). Anbieter-HTTP ist gemockt, kein
echter Netzverkehr. Erfolg endet im bestehenden Magic-Link-Vertrag (PR #93):
/login#login=<token> -> /api/login/verify mit Nonce 200, mit fremder Nonce 409."""

from __future__ import annotations

import base64
import hashlib
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from agent import billing_routes, oauth_routes
from agent.billing import db
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env)
from .test_billing import _paid_wallet
from .test_billing_r4 import _r4_env  # noqa: F401


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def oauth_env(monkeypatch):
    for p in ("GOOGLE", "GITHUB"):
        monkeypatch.setenv(f"{p}_OAUTH_CLIENT_ID", f"{p.lower()}-client-id")
        monkeypatch.setenv(f"{p}_OAUTH_CLIENT_SECRET", "test-secret-do-not-use")


class FakeProvider:
    """Ersetzt oauth_routes._http_json; merkt sich jede Anfrage."""

    def __init__(self, google=None, github_user=None, github_emails=None, token=None):
        self.calls: list[tuple[str, dict | None, dict | None]] = []
        self.google = google if google is not None else {
            "sub": "g-123", "email": "Kim@Example.com", "email_verified": True}
        self.github_user = github_user if github_user is not None else {"id": 4242}
        self.github_emails = github_emails if github_emails is not None else [
            {"email": "kim@example.com", "primary": True, "verified": True}]
        self.token = token if token is not None else {"access_token": "at_test", "token_type": "bearer"}

    def __call__(self, url, *, data=None, headers=None, timeout=10.0):
        self.calls.append((url, data, headers))
        if url in (oauth_routes.PROVIDERS["google"]["token"], oauth_routes.PROVIDERS["github"]["token"]):
            return self.token
        if url == oauth_routes.PROVIDERS["google"]["userinfo"]:
            return self.google
        if url == oauth_routes.PROVIDERS["github"]["user"]:
            return self.github_user
        if url == oauth_routes.PROVIDERS["github"]["emails"]:
            return self.github_emails
        raise AssertionError("unexpected url " + url)


@pytest.fixture
def fake(monkeypatch):
    f = FakeProvider()
    monkeypatch.setattr(oauth_routes, "_http_json", f)
    return f


def _start(client, provider="google", next_path=None, wallet=None, ip=None):
    headers = {}
    if wallet:
        headers["x-wallet-token"] = wallet
    if ip:
        headers["x-forwarded-for"] = ip
    body = {} if next_path is None else {"next": next_path}
    return client.post(f"/api/auth/{provider}/start", json=body, headers=headers)


def _state_of(authorize_url: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))


def _callback(client, provider, state, code="code_test", **extra):
    params = {"state": state, "code": code, **extra}
    return client.get(f"/api/auth/{provider}/callback", params=params, follow_redirects=False)


def _flow(client, provider="google", next_path=None, wallet=None):
    r = _start(client, provider, next_path, wallet)
    assert r.status_code == 200, r.text
    d = r.json()
    q = _state_of(d["authorize_url"])
    cb = _callback(client, provider, q["state"])
    return d, q, cb


def _fragment(location: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).fragment))


# ----- Konfiguration --------------------------------------------------------------
def test_providers_list_reflects_env(client, monkeypatch):
    for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
              "GITHUB_OAUTH_CLIENT_ID", "GITHUB_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    assert client.get("/api/auth/providers").json() == {"google": False, "github": False, "email": False}
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    assert client.get("/api/auth/providers").json()["google"] is False  # nur halbes Paar
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    assert client.get("/api/auth/providers").json() == {"google": True, "github": False, "email": True}


def test_start_and_callback_503_without_config(client, monkeypatch):
    monkeypatch.delenv("GITHUB_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", "id")
    assert _start(client, "github").status_code == 503
    assert client.get("/api/auth/github/callback", params={"state": "x", "code": "y"},
                      follow_redirects=False).status_code == 503


def test_unknown_provider_404(client, oauth_env):
    assert _start(client, "facebook").status_code == 404


def test_start_returns_pkce_authorize_url(client, oauth_env):
    for provider, host, scope in (("google", "accounts.google.com", "openid email"),
                                  ("github", "github.com", "user:email")):
        d = _start(client, provider).json()
        assert d["login_nonce"].startswith("vhn_")
        u = urllib.parse.urlsplit(d["authorize_url"])
        assert u.scheme == "https" and u.netloc == host
        q = _state_of(d["authorize_url"])
        assert q["client_id"] == f"{provider}-client-id"
        assert q["redirect_uri"] == f"https://vh.test/api/auth/{provider}/callback"
        assert q["scope"] == scope and q["response_type"] == "code"
        assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
        assert len(q["state"]) >= 40
        assert "test-secret" not in d["authorize_url"]


def test_pkce_verifier_matches_challenge(client, oauth_env, fake):
    d, q, cb = _flow(client, "github")
    assert cb.status_code == 302
    token_call = next(c for c in fake.calls if c[0] == oauth_routes.PROVIDERS["github"]["token"])
    verifier = token_call[1]["code_verifier"]
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expect
    assert token_call[1]["redirect_uri"] == "https://vh.test/api/auth/github/callback"


def test_start_rate_limited(client, oauth_env):
    for _ in range(billing_routes.LOGIN_IP_LIMIT):
        assert _start(client, ip="198.51.100.7").status_code == 200
    assert _start(client, ip="198.51.100.7").status_code == 429
    assert _start(client, ip="198.51.100.8").status_code == 200


# ----- state ----------------------------------------------------------------------
def test_state_single_use(client, oauth_env, fake):
    d = _start(client).json()
    state = _state_of(d["authorize_url"])["state"]
    first = _callback(client, "google", state)
    assert "#login=" in first.headers["location"]
    again = _callback(client, "google", state)
    assert again.status_code == 302 and again.headers["location"] == "/login#error=state"


def test_state_wrong_or_missing(client, oauth_env, fake):
    assert _callback(client, "google", "made-up-state").headers["location"] == "/login#error=state"
    r = client.get("/api/auth/google/callback", params={"code": "c"}, follow_redirects=False)
    assert r.headers["location"] == "/login#error=state"
    assert fake.calls == []  # ohne gültigen state nie Code tauschen


def test_state_bound_to_provider(client, oauth_env, fake):
    d = _start(client, "google").json()
    state = _state_of(d["authorize_url"])["state"]
    assert _callback(client, "github", state).headers["location"] == "/login#error=state"
    # und danach auch für Google verbraucht
    assert _callback(client, "google", state).headers["location"] == "/login#error=state"


def test_state_expired(client, oauth_env, fake, monkeypatch):
    d = _start(client).json()
    state = _state_of(d["authorize_url"])["state"]
    real = db.consume_oauth_state
    monkeypatch.setattr(db, "consume_oauth_state",
                        lambda p, s, now=None: real(p, s, now=__import__("time").time() + db.OAUTH_STATE_TTL_S + 1))
    assert _callback(client, "google", state).headers["location"] == "/login#error=state"
    assert fake.calls == []


def test_provider_error_param(client, oauth_env, fake):
    d = _start(client, next_path="/r/abc").json()
    state = _state_of(d["authorize_url"])["state"]
    r = client.get("/api/auth/google/callback", params={"state": state, "error": "access_denied"},
                   follow_redirects=False)
    assert r.headers["location"] == "/login?next=/r/abc#error=denied"
    assert fake.calls == []


def test_token_exchange_failure(client, oauth_env, fake):
    fake.token = {"error": "bad_verification_code"}  # GitHub: 200 mit error
    _, _, cb = _flow(client, "github")
    assert cb.headers["location"] == "/login#error=provider"


# ----- verifizierte E-Mail --------------------------------------------------------
@pytest.mark.parametrize("verified", [False, "false", None])
def test_google_unverified_rejected(client, oauth_env, fake, verified):
    fake.google = {"sub": "g-1", "email": "kim@example.com", "email_verified": verified}
    _, _, cb = _flow(client, "google")
    assert cb.headers["location"] == "/login#error=email_unverified"
    assert "#login=" not in cb.headers["location"]


@pytest.mark.parametrize("emails", [
    [{"email": "kim@example.com", "primary": True, "verified": False}],
    [{"email": "kim@example.com", "primary": False, "verified": True}],
    [],
])
def test_github_needs_primary_and_verified(client, oauth_env, fake, emails):
    fake.github_emails = emails
    _, _, cb = _flow(client, "github")
    assert cb.headers["location"] == "/login#error=email_unverified"


def test_github_picks_primary_verified(client, oauth_env, fake):
    fake.github_emails = [
        {"email": "other@example.com", "primary": False, "verified": True},
        {"email": "Main@Example.com", "primary": True, "verified": True},
    ]
    d, _, cb = _flow(client, "github")
    token = _fragment(cb.headers["location"])["login"]
    r = client.get("/api/login/verify", params={"token": token, "nonce": d["login_nonce"]})
    assert r.status_code == 200 and r.json()["email_masked"] == "m***@e***.com"


# ----- Erfolg = bestehender Magic-Link-Vertrag ------------------------------------
@pytest.mark.parametrize("provider", ["google", "github"])
def test_success_login_with_matching_nonce(client, oauth_env, fake, provider):
    d, _, cb = _flow(client, provider, next_path="/r/room-1?x=1")
    assert cb.status_code == 302
    loc = cb.headers["location"]
    assert loc.startswith("/login?next=/r/room-1%3Fx%3D1#login=vhl_")
    token = _fragment(loc)["login"]
    r = client.get("/api/login/verify", params={"token": token, "nonce": d["login_nonce"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wallet_token"].startswith("vhw_") and body["email_verified"] is True
    acc = db.account_for_token(body["wallet_token"])
    row = db.account(acc)
    assert row["email"] == "kim@example.com" and row["email_verified_at"]
    with db.connect() as conn:
        ident = conn.execute("SELECT provider, subject, email FROM oauth_identities").fetchone()
    assert tuple(ident) == (provider, "g-123" if provider == "google" else "4242", "kim@example.com")


def test_foreign_nonce_needs_confirmation(client, oauth_env, fake):
    """Login-CSRF: Angreifer schickt dem Opfer seinen eigenen #login=-Link. Ohne
    die Nonce des anfordernden Browsers -> 409 confirm_required, Link bleibt gültig."""
    _, _, cb = _flow(client, "google")
    token = _fragment(cb.headers["location"])["login"]
    victim_nonce = _start(client).json()["login_nonce"]  # Opfer hat eigene, andere Nonce
    r = client.get("/api/login/verify", params={"token": token, "nonce": victim_nonce})
    assert r.status_code == 409 and r.json()["error"] == "confirm_required"
    assert r.json()["email_masked"] == "k***@e***.com"
    assert client.get("/api/login/verify", params={"token": token}).status_code == 409


def test_same_email_same_account_as_mail_login(client, oauth_env, fake):
    d1, _, cb1 = _flow(client, "google")
    w1 = client.get("/api/login/verify", params={
        "token": _fragment(cb1.headers["location"])["login"], "nonce": d1["login_nonce"]}).json()
    d2, _, cb2 = _flow(client, "github")
    w2 = client.get("/api/login/verify", params={
        "token": _fragment(cb2.headers["location"])["login"], "nonce": d2["login_nonce"]}).json()
    assert db.account_for_token(w1["wallet_token"]) == db.account_for_token(w2["wallet_token"])


def test_requesting_wallet_is_linked(client, oauth_env, fake):
    """X-Wallet-Token beim Start = Anforderer: sein (unbestätigtes) Guthaben wandert ins Konto."""
    wallet = _paid_wallet(client, "cs_oauth_1", 2000, "kim@example.com")["wallet_token"]
    d, _, cb = _flow(client, "google", wallet=wallet)
    token = _fragment(cb.headers["location"])["login"]
    r = client.get("/api/login/verify", params={"token": token, "nonce": d["login_nonce"]},
                   headers={"x-wallet-token": wallet})
    assert r.status_code == 200 and r.json()["wallet_linked"] is True
    assert r.json()["balance_eur"] == 20.0


def test_foreign_wallet_not_linked(client, oauth_env, fake):
    """Wallet, das NICHT gestartet hat, wird beim Einlösen nie angefasst."""
    other = _paid_wallet(client, "cs_oauth_2", 1000, "victim@example.com")["wallet_token"]
    d, _, cb = _flow(client, "google")
    token = _fragment(cb.headers["location"])["login"]
    r = client.get("/api/login/verify", params={"token": token, "nonce": d["login_nonce"]},
                   headers={"x-wallet-token": other})
    assert r.status_code == 200 and r.json()["wallet_linked"] is False
    assert db.account_for_token(other) is not None


def test_no_secrets_in_logs(client, oauth_env, fake, caplog):
    caplog.set_level("DEBUG")
    d, q, cb = _flow(client, "google")
    token = _fragment(cb.headers["location"])["login"]
    # nur unsere Logger (der Testclient protokolliert selbst die Anfrage-URL)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("voicehook"))
    assert "[oauth] google verified" in text  # Positivkontrolle: es wurde geloggt
    for secret in ("test-secret-do-not-use", "code_test", "at_test", q["state"], token, d["login_nonce"]):
        assert secret not in text


# ----- next-Validierung -----------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("/r/abc", "/r/abc"),
    ("/aufladen?topup=1", "/aufladen?topup=1"),
    ("https://evil.example/x", "/aufladen"),
    ("//evil.example/x", "/aufladen"),
    ("/\\evil.example", "/aufladen"),
    ("javascript:alert(1)", "/aufladen"),
    ("r/abc", "/aufladen"),
    ("/login?next=/x", "/aufladen"),
    ("/x\r\nSet-Cookie: a=b", "/aufladen"),
    ("", "/aufladen"),
    (None, "/aufladen"),
    ("/" + "a" * 600, "/aufladen"),
])
def test_safe_next(value, expected):
    assert oauth_routes.safe_next(value) == expected


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example", "javascript:alert(1)"])
def test_bad_next_falls_back_in_redirect(client, oauth_env, fake, bad):
    _, _, cb = _flow(client, "google", next_path=bad)
    loc = cb.headers["location"]
    assert loc.startswith("/login#login=")  # Fallback /aufladen, kein next im Redirect
    assert "evil" not in loc and "javascript" not in loc
