"""VH_FREE_EXEMPT_ACCOUNTS (Oliver 02.10.): die Owner-Ausnahme vom Gratis-Limit hängt am
ANGEMELDETEN Konto ("github:<id>" / "google:<sub>", stabile Anbieter-IDs aus
oauth_identities.subject), nicht an IP-/Browser-Hashes.

  - eingeloggt + gelistet -> frei (kein 402, /api/me exempt, Worker bucht nichts, kein Ende)
  - eingeloggt, nicht gelistet -> normal limitiert
  - nicht eingeloggt im selben IP-Bucket/Browser wie der Owner, ohne VH_FREE_EXEMPT_KEYS
    -> limitiert
  - Wallet ohne bestätigte Mail (Stripe-Kontaktmail = Owner-Mail) -> limitiert
  - /api/me == Worker (Topic free.state)
Positivkontrolle: auf origin/main gibt es VH_FREE_EXEMPT_ACCOUNTS nicht, die "frei"-Tests
sind dort rot (402 / exempt fehlt / Worker beendet)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import agent.worker as w
from agent import freetier
from agent.billing import db
from agent.server import app

from .test_billing import _paid_wallet
from .test_free_state_single_source import _published
from .test_freetier import (  # noqa: F401  (Fixture _env wirkt autouse über den Import)
    ANON_A,
    ANON_B,
    _env,
    _live,
    _rt,
    _run_free,
    _use,
)

ENV = "VH_FREE_EXEMPT_ACCOUNTS"
OWNER_IP = "1.1.1.1"
OWNER_MAIL = "owner@example.com"
OWNER_GH = "424242"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_exempt(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("VH_FREE_EXEMPT_KEYS", raising=False)


def _login(email: str, provider: str = "github", subject: str = OWNER_GH) -> tuple[str, str]:
    """Wie der OAuth-Callback + /api/login/verify: Identität vermerken, Konto mit
    bestätigter Mail, Wallet-Token für den Browser. -> (account_id, wallet_token)."""
    db.record_oauth_identity(provider, subject, email)
    acc, token, _, _ = db.login_verified_email(email)
    return acc, token


def _h(anon=ANON_A, ip=OWNER_IP, wallet=None) -> dict:
    h = {"x-forwarded-for": ip, "x-anon-id": anon}
    if wallet:
        h["x-wallet-token"] = wallet
    return h


def _me(client, **kw) -> dict:
    return client.get("/api/me", headers=_h(**kw)).json()["free"]


# ----- Parsing ------------------------------------------------------------------
def test_parse_accounts_ignores_broken(monkeypatch):
    monkeypatch.setenv(ENV, f" github:{OWNER_GH} ,, google:1234567890 ,kaputt, x:1, github:, github:a b")
    assert freetier.exempt_accounts() == frozenset({("github", OWNER_GH), ("google", "1234567890")})
    monkeypatch.setenv(ENV, "")
    assert freetier.exempt_accounts() == frozenset()


# ----- eingeloggt + gelistet -> frei ---------------------------------------------
def test_logged_in_listed_is_free(client, monkeypatch):
    _, token = _login(OWNER_MAIL)
    _use(ANON_A, ip=OWNER_IP)                                     # Tagestopf voll
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    assert _me(client, wallet=token) == {"eur_left": 1.0, "eur_per_day": 1.0, "exempt": True}
    r = _live(client, ANON_A, ip=OWNER_IP, wallet=token)
    assert r.status_code == 200, r.text
    assert freetier.room_account(r.json()["room"]) == db.account_for_token(token)


def test_google_sub_works_too(client, monkeypatch):
    _, token = _login(OWNER_MAIL, provider="google", subject="109876543210")
    _use(ANON_A, ip=OWNER_IP)
    monkeypatch.setenv(ENV, "google:109876543210")
    assert _me(client, wallet=token).get("exempt") is True
    assert _live(client, ANON_A, ip=OWNER_IP, wallet=token).status_code == 200


def test_logged_in_listed_from_any_ip_and_browser(client, monkeypatch):
    """Konto statt Hash: neue IP, neuer Browser, trotzdem frei."""
    _, token = _login(OWNER_MAIL)
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    _use(ANON_B, ip="5.5.5.5")
    assert _live(client, ANON_B, ip="5.5.5.5", wallet=token).status_code == 200


def test_worker_logged_in_listed_no_booking_no_end(client, monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")             # 1 Ereignis würde reichen
    _, token = _login(OWNER_MAIL)
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    r = _live(client, ANON_A, ip=OWNER_IP, wallet=token)
    assert r.status_code == 200
    room = r.json()["room"]
    ctx, _ = _run_free(monkeypatch, room=room, humans=1, wait_s=0.2, metrics=[_rt(1000, 500)] * 3)
    ctx.shutdown.assert_not_called()
    assert freetier.used_ueur(freetier.identity_keys(ANON_A, OWNER_IP)) == 0


# ----- eingeloggt, nicht gelistet -> normal ----------------------------------------
def test_logged_in_not_listed_is_limited(client, monkeypatch):
    _, token = _login("other@example.com", subject="777")
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    _use(ANON_B, ip="2.2.2.2")
    assert "exempt" not in _me(client, anon=ANON_B, ip="2.2.2.2", wallet=token)
    r = _live(client, ANON_B, ip="2.2.2.2", wallet=token)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"


def test_worker_logged_in_not_listed_ends(client, monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    _, token = _login("other@example.com", subject="777")
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    r = _live(client, ANON_B, ip="2.2.2.2", wallet=token)
    assert r.status_code == 200                                   # Topf noch voll
    ctx, _ = _run_free(monkeypatch, room=r.json()["room"], humans=1, wait_s=0.2,
                       metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    assert freetier.used_ueur(freetier.identity_keys(ANON_B, "2.2.2.2")) == 10_000


# ----- nicht eingeloggt -> nie Ausnahme ---------------------------------------------
def test_not_logged_in_same_ip_and_browser_as_owner_is_limited(client, monkeypatch):
    _login(OWNER_MAIL)                                            # Owner existiert, ist gelistet
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    _use(ANON_A, ip=OWNER_IP)
    assert "exempt" not in _me(client)                            # gleicher Bucket, ohne Token
    assert _live(client, ANON_A, ip=OWNER_IP).status_code == 402
    # ungültiges Token zählt wie keins
    assert _live(client, ANON_A, ip=OWNER_IP, wallet="nope").status_code == 402


def test_unverified_wallet_with_owner_contact_mail_is_limited(client, monkeypatch):
    """Stripe-Kontaktmail ist ungeprüft: Wallet ohne Login bekommt die Ausnahme nie,
    auch wenn die Kontaktmail der Owner-Mail gleicht."""
    db.record_oauth_identity("github", OWNER_GH, OWNER_MAIL)
    wallet = _paid_wallet(client, session_id="cs_owner_fake", email=OWNER_MAIL)
    acc = db.account_for_token(wallet["wallet_token"])
    assert db.account(acc)["email_verified_at"] is None
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    assert not freetier.account_exempt(acc)
    assert "exempt" not in _me(client, wallet=wallet["wallet_token"])


def test_empty_env_no_db_access(monkeypatch):
    monkeypatch.setattr(db, "connect", lambda: (_ for _ in ()).throw(AssertionError("db read")))
    assert not freetier.account_exempt("acc_x")


def test_db_error_means_no_exemption(monkeypatch):
    monkeypatch.setenv(ENV, f"github:{OWNER_GH}")

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "connect", boom)
    assert not freetier.account_exempt("acc_x")


# ----- Frontend == Worker ---------------------------------------------------------
@pytest.mark.parametrize("listed", [True, False])
def test_api_me_equals_worker_state(client, monkeypatch, listed):
    _, token = _login(OWNER_MAIL)
    if listed:
        monkeypatch.setenv(ENV, f"github:{OWNER_GH}")
    _use(ANON_A, ip=OWNER_IP, eur=0.4)
    me = _me(client, wallet=token)
    r = _live(client, ANON_A, ip=OWNER_IP, wallet=token)
    assert r.status_code == 200
    ctx, _ = _run_free(monkeypatch, room=r.json()["room"], humans=1, wait_s=0.1)
    states = _published(ctx, w.TOPIC_FREE_STATE)
    assert states, "Worker hat keinen free.state publiziert"
    worker = states[0]
    assert me["eur_left"] == worker["eur_left"] == (1.0 if listed else 0.6)
    assert me["eur_per_day"] == worker["eur_per_day"]
    assert bool(me.get("exempt")) == worker["exempt"] == listed
