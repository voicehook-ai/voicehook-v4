"""Billing Runde 4 (Oliver 01.10.): erst Gratis dann Guthaben, /api/me, Magic-Link-Login,
/api/invite-room, 5-Minuten-Warnung im Worker. Keine echten Stripe-/Resend-Calls."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import agent.worker as w
from agent import billing_routes, freetier, relay
from agent.billing import db, mail
from agent.server import app

from .test_billing import _env as _billing_env  # noqa: F401  (Stripe/LiveKit-Env, kein Dispatch)
from .test_billing import _paid_wallet, _post_webhook
from .test_wallet_worker import _account
from .test_wallet_worker import _run as _run_worker
from .test_worker import _metric

ANON = "anon-r4r4r4r4-0001"
H = {"x-anon-id": ANON, "x-forwarded-for": "8.8.8.8"}


@pytest.fixture(autouse=True)
def _r4_env(monkeypatch):
    import agent.server as srv
    monkeypatch.setattr(srv, "_HOST_LIMIT", 10_000)
    for k in ("RESEND_API_KEY", "MAIL_FROM", "VH_LOW_BALANCE_WARN_SECONDS", "VH_FREE_TICK_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    billing_routes._LOGIN_HITS.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def outbox(monkeypatch):
    """Resend-Versand abgefangen: Liste (an, Nachricht)."""
    sent: list[tuple[str, dict]] = []
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    monkeypatch.setattr(mail, "send", lambda to, msg, **k: sent.append((to, msg)))
    return sent


def _jwt_payload(token: str) -> dict:
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


# ----- Reihenfolge: erst Gratis, dann Guthaben ------------------------------------
def test_host_call_with_wallet_and_free_left_registers_both(client):
    wl = _paid_wallet(client, "cs_both")
    r = client.post("/api/host-call", json={"identity": "u"}, headers={**H, "x-wallet-token": wl["wallet_token"]})
    assert r.status_code == 200
    room = r.json()["room"]
    assert freetier.room_keys(room)[0] == "normal"                 # Gratis zählt zuerst
    assert db.room_wallet(room)[1] == "normal"                     # Wallet zahlt danach


def test_host_call_free_used_up_wallet_pays_alone(client):
    freetier.add_ueur(freetier.identity_keys(ANON, "8.8.8.8"), 1_000_000)
    wl = _paid_wallet(client, "cs_alone")
    r = client.post("/api/host-call", json={"identity": "u"}, headers={**H, "x-wallet-token": wl["wallet_token"]})
    assert r.status_code == 200
    assert freetier.room_keys(r.json()["room"]) is None and db.room_wallet(r.json()["room"]) is not None
    # Gegenprobe: ohne Wallet -> 402 free_limit mit Aufladen-Link
    r2 = client.post("/api/host-call", json={"identity": "u"}, headers=H)
    assert r2.status_code == 402 and r2.json()["detail"]["topup_url"] == "/aufladen"


def test_worker_does_not_charge_wallet_during_free_phase(monkeypatch):
    acc = _account(1000, "cs_free_phase")
    db.bind_room("r1", acc, "normal")
    freetier.register_room("r1", "normal", freetier.identity_keys(ANON, "8.8.8.8"))
    _run_worker(monkeypatch, live_mode=False, metrics=[_metric("STTMetrics", audio_duration=600.0)])
    assert db.balance_ueur(acc) == 10_000_000                      # Gratis-Teil: Guthaben unberührt


# ----- GET /api/me ----------------------------------------------------------------
def test_me_without_wallet(client):
    r = client.get("/api/me", headers=H).json()
    assert r["free"] == {"eur_left": 1.0, "eur_per_day": 1.0}
    assert r["balance_eur"] is None and r["email_masked"] is None


def test_me_with_wallet_shows_balance_and_masked_mail(client):
    wl = _paid_wallet(client, "cs_me", amount_cents=2000, email="Victor@Example.com")
    freetier.add_ueur(freetier.identity_keys(ANON, "8.8.8.8"), 270_001)  # 0,270001 EUR
    r = client.get("/api/me", headers={**H, "x-wallet-token": wl["wallet_token"]}).json()
    assert r["balance_eur"] == 20.0
    assert r["email_masked"] == "v***@e***.com" and "victor" not in json.dumps(r)
    assert r["email_verified"] is False                            # Stripe-Mail ist unbestätigt
    assert r["free"] == {"eur_left": 0.72, "eur_per_day": 1.0}   # abgerundet, nie mehr als da


def test_me_with_bogus_token_is_anonymous(client):
    r = client.get("/api/me", headers={**H, "x-wallet-token": "vhw_bogus"})
    assert r.status_code == 200 and r.json()["balance_eur"] is None


# ----- POST /api/invite-room ------------------------------------------------------
def test_invite_room_counts_free_and_returns_contract(client):
    r = client.post("/api/invite-room", json={"identity": "host-1"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"token", "url", "room", "identity", "invite_url", "expires_in"}
    assert body["invite_url"].startswith(f"https://vh.test/r/{body['room']}?invite=")
    assert freetier.room_keys(body["room"])[0] == "normal"
    claims = _jwt_payload(body["token"])
    assert "roomConfig" not in claims                              # Join selbst dispatcht nicht
    assert (claims.get("attributes") or {}).get("vh.role") != "agent"  # Gastgeber ist kein Agent


def test_invite_room_402_when_free_used_and_no_wallet_then_wallet_binds(client):
    freetier.add_ueur(freetier.identity_keys(ANON, "8.8.8.8"), 1_000_000)
    r = client.post("/api/invite-room", json={"identity": "h"}, headers=H)
    assert r.status_code == 402 and r.json()["detail"]["error"] == "free_limit"
    wl = _paid_wallet(client, "cs_inv")
    ok = client.post("/api/invite-room", json={"identity": "h"}, headers={**H, "x-wallet-token": wl["wallet_token"]})
    assert ok.status_code == 200
    assert db.room_wallet(ok.json()["room"]) == (db.account_for_token(wl["wallet_token"]), "normal")


def test_invite_room_dispatches_voice_ai(client, monkeypatch):
    import agent.server as srv
    calls = []
    monkeypatch.setattr(srv, "_ensure_agent_dispatched", lambda room, name="voice-ai": calls.append((room, name)))
    r = client.post("/api/invite-room", json={"identity": "h"}, headers=H)
    for _ in range(50):
        if calls:
            break
        time.sleep(0.01)
    assert calls == [(r.json()["room"], "voice-ai")]


# ----- Magic-Link-Login -------------------------------------------------------------
def _link_token(outbox) -> str:
    text = outbox[-1][1]["text"]
    return text.split("#login=")[1].split()[0]


def test_login_without_resend_key_is_503(client):
    r = client.post("/api/login", json={"email": "a@b.de"})
    assert r.status_code == 503 and r.json()["detail"] == "login_unavailable"


def test_login_sends_link_same_answer_for_known_and_unknown(client, outbox):
    _paid_wallet(client, "cs_known", email="known@x.de")
    a = client.post("/api/login", json={"email": "Known@X.de"}).json()
    b = client.post("/api/login", json={"email": "nobody@x.de"}).json()
    assert a.pop("login_nonce") != b.pop("login_nonce")                     # je Anfrage zufällig
    assert a == b == {"sent": True, "expires_in": 900}                      # nichts leakt
    assert [to for to, _ in outbox] == ["known@x.de", "nobody@x.de"]
    assert "https://vh.test/aufladen#login=vhl_" in outbox[0][1]["text"]
    assert "—" not in outbox[0][1]["text"] and "–" not in outbox[0][1]["text"]


def test_login_invalid_email_400(client, outbox):
    assert client.post("/api/login", json={"email": "kein-at"}).status_code == 400
    assert outbox == []


def test_login_rate_limit_per_mail_and_per_ip(client, outbox):
    for _ in range(3):
        assert client.post("/api/login", json={"email": "rl@x.de"},
                           headers={"x-forwarded-for": "7.7.7.1"}).status_code == 200
    assert client.post("/api/login", json={"email": "rl@x.de"},
                       headers={"x-forwarded-for": "7.7.7.1"}).status_code == 429   # je Adresse + IP
    billing_routes._LOGIN_HITS.clear()
    for i in range(5):
        assert client.post("/api/login", json={"email": f"ip{i}@x.de"},
                           headers={"x-forwarded-for": "6.6.6.6"}).status_code == 200
    assert client.post("/api/login", json={"email": "ip9@x.de"},
                       headers={"x-forwarded-for": "6.6.6.6"}).status_code == 429   # je IP


def test_verify_once_and_expiry(client, outbox):
    _paid_wallet(client, "cs_v", amount_cents=1500, email="me@x.de")
    nonce = client.post("/api/login", json={"email": "me@x.de"}).json()["login_nonce"]
    tok = _link_token(outbox)
    r = client.get("/api/login/verify", params={"token": tok, "nonce": nonce})
    assert r.status_code == 200 and r.json()["balance_eur"] == 15.0
    assert r.json()["wallet_token"].startswith("vhw_") and "#r=vhr_" in r.json()["recovery_url"]
    assert client.get("/api/login/verify", params={"token": tok, "nonce": nonce}).status_code == 400
    old, _ = db.create_login_link("me@x.de", now=time.time() - db.LOGIN_TTL_S - 1)
    assert client.get("/api/login/verify", params={"token": old}).status_code == 400   # abgelaufen
    assert client.get("/api/login/verify", params={"token": "vhl_falsch"}).status_code == 400


def test_stripe_mail_is_stored_unverified(client):
    wl = _paid_wallet(client, "cs_unv", email="U@x.de")
    row = db.account(db.account_for_token(wl["wallet_token"]))
    assert row["email"] == "u@x.de" and row["email_verified_at"] is None


def test_attacker_typing_victim_mail_never_keeps_access_after_victim_login(client, outbox):
    """PR #88 critical, weitergedacht: Angreifer zahlt mit der Opfer-Mail (Konto
    unbestätigt, er hat das Token). Loggt das Opfer sich per Mail ein, verliert der
    Angreifer jeden Zugriff; er erreicht nie ein Konto, dessen Mail er nicht bestätigt hat."""
    att = _paid_wallet(client, "cs_att", amount_cents=1000, email="victim@x.de")
    n = client.post("/api/login", json={"email": "victim@x.de"}).json()["login_nonce"]
    v = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n}).json()
    assert db.account_for_token(att["wallet_token"]) is None                # Angreifer-Token tot
    assert db.account_for_token(att["recovery_url"].split("#r=")[1], "recovery") is None
    assert db.account_for_token(v["wallet_token"]) is not None              # Opfer drin
    # Später zahlt der Angreifer erneut mit der Opfer-Mail: neues, getrenntes Konto
    att2 = _paid_wallet(client, "cs_att2", amount_cents=1000, email="victim@x.de")
    acc_v = db.account_for_token(v["wallet_token"])
    assert db.account_for_token(att2["wallet_token"]) != acc_v
    assert client.get("/api/wallet", headers={"x-wallet-token": att2["wallet_token"]}).json()["balance_eur"] == 10.0
    # Nächster Opfer-Login führt es ins bestätigte Konto über, Angreifer-Token wieder tot
    n = client.post("/api/login", json={"email": "victim@x.de"}).json()["login_nonce"]
    v2 = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n}).json()
    assert db.account_for_token(v2["wallet_token"]) == acc_v and v2["balance_eur"] == 20.0
    assert db.account_for_token(att2["wallet_token"]) is None


def test_attacker_requesting_link_for_victim_cannot_bind_own_wallet(client, outbox):
    """Angreifer fordert mit SEINEM Wallet einen Link für die Opfer-Mail an. Der Link
    geht ans Opfer; klickt es in seinem Browser, wird nie das Angreifer-Konto bestätigt."""
    att = _paid_wallet(client, "cs_att3", email="att@x.de")
    client.post("/api/login", json={"email": "victim2@x.de"}, headers={"x-wallet-token": att["wallet_token"]})
    # Opfer-Browser (ohne Nonce), Opfer bestätigt sogar die eigene Adresse
    v = client.get("/api/login/verify", params={"token": _link_token(outbox), "confirm": 1}).json()
    acc_att = db.account_for_token(att["wallet_token"])
    assert acc_att is not None and db.account_for_token(v["wallet_token"]) != acc_att
    assert db.account(acc_att)["email_verified_at"] is None


def test_same_browser_confirmation_keeps_wallet_logged_in(client, outbox):
    me = _paid_wallet(client, "cs_same", amount_cents=1000, email="same@x.de")
    acc = db.account_for_token(me["wallet_token"])
    n = client.post("/api/login", json={"email": "same@x.de"},
                    headers={"x-wallet-token": me["wallet_token"]}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n},
                   headers={"x-wallet-token": me["wallet_token"]}).json()
    assert db.account_for_token(r["wallet_token"]) == acc
    assert db.account_for_token(me["wallet_token"]) == acc                  # bleibt angemeldet
    assert db.account(acc)["email_verified_at"] is not None


def test_new_mail_without_account_creates_empty_verified_account(client, outbox):
    n = client.post("/api/login", json={"email": "new@x.de"}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n}).json()
    assert r["balance_eur"] == 0.0 and r["can_call"] is False
    assert db.account(db.account_for_token(r["wallet_token"]))["email_verified_at"]


def test_refund_after_merge_hits_merged_account(client, outbox):
    _paid_wallet(client, "cs_m1", amount_cents=1000, email="m@x.de")
    _paid_wallet(client, "cs_m2", amount_cents=1000, email="m@x.de")
    n = client.post("/api/login", json={"email": "m@x.de"}).json()["login_nonce"]
    r = client.get("/api/login/verify", params={"token": _link_token(outbox), "nonce": n}).json()
    assert r["balance_eur"] == 20.0                                       # beide zusammengeführt
    ev = {"id": "evt_rf", "type": "charge.refunded", "data": {"object": {
        "id": "ch_1", "payment_intent": "pi_cs_m2", "amount_refunded": 1000}}}
    assert _post_webhook(client, ev).status_code == 200
    acc = db.account_for_token(r["wallet_token"])
    assert db.balance_ueur(acc) == 10_000_000


def test_resend_request_shape(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id":"x"}'

    def _open(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.headers["Authorization"]
        seen["body"] = json.loads(req.data)
        return _Resp()

    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")
    monkeypatch.setattr(mail.urllib.request, "urlopen", _open)
    mail.send("a@b.de", mail.login_message("https://vh.test/aufladen#login=vhl_x", 15))
    assert seen["url"] == "https://api.resend.com/emails" and seen["auth"] == "Bearer re_test_do_not_use"
    assert seen["body"]["from"] == mail.DEFAULT_FROM and seen["body"]["to"] == ["a@b.de"]
    monkeypatch.delenv("RESEND_API_KEY")
    with pytest.raises(mail.MailError):
        mail.send("a@b.de", {"subject": "s", "text": "t", "html": "h"})


def test_mail_failure_is_502(client, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_do_not_use")

    def _boom(*a, **k):
        raise mail.MailError("resend 500")

    monkeypatch.setattr(mail, "send", _boom)
    assert client.post("/api/login", json={"email": "a@b.de"}).status_code == 502


# ----- 5-Minuten-Warnung ------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_low_balance_free_only_uses_free_rest_in_euro():
    """Restzeit = Gratis-Rest (µEUR) / Verbrauch; ohne bekannten Verbrauch keine Schätzung."""
    free = w.FreeBudget("r", "normal")
    free.counting, free.left_ueur = True, 299_000
    clock = _Clock()
    watch = w.LowBalanceWatch(free, w.WalletCharger("r", "normal"), clock=clock)
    assert watch.seconds_left() is None                             # Verbrauch unbekannt
    clock.t += 60
    watch._costs.append((clock.t, 60_000))                          # 1 000 µEUR/s
    assert watch.seconds_left() == pytest.approx(299.0)
    free.left_ueur = 301_000
    assert watch.seconds_left() == pytest.approx(301.0)


def test_low_balance_wallet_projects_from_last_3_minutes():
    acc = _account(1000, "cs_lb")                                   # 10 EUR
    db.bind_room("lb", acc, "normal")
    wc = w.WalletCharger("lb", "normal")
    wc.lookup()
    clock = _Clock()
    watch = w.LowBalanceWatch(None, wc, clock=clock)
    watch.add_cost(1.0)
    assert watch.seconds_left() is None                             # < 1 min beobachtet: keine Schätzung
    clock.t += 60
    rate = watch.burn_ueur_per_s()
    assert rate == pytest.approx(w.billing_pricing.charge_ueur(1.0, "normal") / 60)
    assert watch.seconds_left() == pytest.approx(10_000_000 / rate)
    clock.t += 400                                                  # Kosten fallen aus dem Fenster
    assert not watch.burn_ueur_per_s()


def test_low_balance_free_plus_wallet_adds_up():
    acc = _account(1000, "cs_lb2")
    db.bind_room("lb2", acc, "normal")
    wc = w.WalletCharger("lb2", "normal")
    wc.lookup()
    free = w.FreeBudget("lb2", "normal")
    free.counting, free.left_ueur = True, 120_000                    # 0,12 EUR Gratis-Rest
    clock = _Clock()
    watch = w.LowBalanceWatch(free, wc, clock=clock)
    clock.t += 60
    watch._costs.append((clock.t, 60_000))                          # 1 000 µEUR/s
    # (0,12 EUR + 10 EUR) / 0,001 EUR/s
    assert watch.seconds_left() == pytest.approx((120_000 + 10_000_000) / 1000)


def test_low_balance_unlimited_room_never_warns():
    watch = w.LowBalanceWatch(None, w.WalletCharger("x", "normal"))
    assert watch.seconds_left() is None
    assert asyncio.run(watch.check()) is None


def test_low_balance_warns_once_with_payload():
    free = w.FreeBudget("r", "live")
    free.counting, free.left_ueur = True, 200_000
    clock = _Clock()
    watch = w.LowBalanceWatch(free, w.WalletCharger("r", "live"), clock=clock)
    clock.t += 60
    watch._costs.append((clock.t, 60_000))                          # 1 000 µEUR/s -> 200 s
    p = asyncio.run(watch.check())
    assert p["kind"] == "low_balance" and p["minutes_left"] == 4 and p["seconds_left"] == 200
    assert p["free_s"] == 200 and p["free_eur"] == 0.2
    assert p["balance_eur"] is None and p["topup_url"].endswith("/aufladen")
    assert asyncio.run(watch.check()) is None                       # einmal pro Call


def test_worker_sends_notice_and_announcement_once(monkeypatch):
    from .test_freetier import _run_free

    # Topf 0,01 EUR; Verbrauch schnell genug, dass die Restzeit < 5 min ist, dann leer.
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    real = w.LowBalanceWatch.__init__

    def _fast(self, *a, **k):
        real(self, *a, **{**k, "min_span_s": 0.05})                 # Hochrechnung schon nach 50 ms
    monkeypatch.setattr(w.LowBalanceWatch, "__init__", _fast)
    freetier.register_room("lb-room", "normal", freetier.identity_keys(ANON, "8.8.8.8"))
    stt = _metric("STTMetrics", audio_duration=60.0)                # 0,024 EUR je Ereignis bei Faktor 3
    ctx, session = _run_free(monkeypatch, room="lb-room", humans=1, wait_s=0.3, live_mode=False,
                             metrics=[_metric("STTMetrics", audio_duration=1.0)] * 4 + [stt], gap_s=0.05)
    pub = ctx.room.local_participant.publish_data
    notices = [c for c in pub.call_args_list if c.kwargs.get("topic") == relay.TOPIC_NOTICE]
    assert len(notices) == 1 and notices[0].kwargs["reliable"] is True
    assert json.loads(notices[0].kwargs["payload"])["kind"] == "low_balance"
    said = [c.args[0] for c in session.say.call_args_list]
    assert said.count(relay.LOW_BALANCE_ANNOUNCEMENT) == 1
    assert said.index(relay.LOW_BALANCE_ANNOUNCEMENT) < said.index(w.FREE_LIMIT_ANNOUNCEMENT)


def test_worker_no_notice_with_plenty_left(monkeypatch):
    from .test_freetier import _run_free

    freetier.register_room("lb-ok", "normal", freetier.identity_keys(ANON, "8.8.8.8"))   # 20 min
    ctx, session = _run_free(monkeypatch, room="lb-ok", humans=1, wait_s=0.3, live_mode=False)
    pub = ctx.room.local_participant.publish_data
    assert not [c for c in pub.call_args_list if c.kwargs.get("topic") == relay.TOPIC_NOTICE]
    session.say.assert_not_called()                                   # Positivkontrolle zum Test oben


def test_announcement_short_no_dashes():
    t = relay.LOW_BALANCE_ANNOUNCEMENT
    assert len(t) <= 60 and "Guthaben" in t and "—" not in t and "–" not in t  # Olli 02.10.: natuerlicher Satz, ohne Domain


def test_speak_notice_live_uses_generate_reply():
    s = SimpleNamespace(say=MagicMock(), generate_reply=MagicMock())
    relay.speak_notice(s, "x", live=True)
    s.generate_reply.assert_called_once()
    s.say.assert_not_called()


def test_publish_notice_swallows_errors():
    room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=AsyncMock(side_effect=OSError)))
    assert asyncio.run(relay.publish_notice(room, {"kind": "low_balance"})) is False
