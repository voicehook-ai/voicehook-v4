"""EINE Gratis-Rechnung für UI und Worker (Oliver 02.10.: "Frontend und Agenten brauchen
die GLEICHEN Werte!!!! Wie kann das Frontend sagen, es ist voll, und das Backend sagt leer?").

freetier.free_state ist die einzige Stelle, die den Gratis-Rest berechnet. /api/me,
/api/free/remaining, die 402-Entscheidung beim Raumanlegen und der Worker (Start-Prüfung,
Buchung, Topic free.state) lesen nur dort. Diese Datei prüft:
  1. gleicher Zustand -> /api/me == Worker-Stand (Topic free.state) == 402-Entscheidung,
     für frisch, angebraucht, persönliches Limit, leerer Topf, Owner, neuer Tag;
  2. Owner (VH_FREE_EXEMPT_KEYS) wird auch im Topf-Pfad nie gesperrt;
  3. Worker meldet das Call-Ende per Topic call_end (Browser beendet sofort);
  4. Import-/Grep-Wächter: keine zweite Rechnung außerhalb von freetier.
Auf origin/main gibt es free_state, free.state und call_end nicht (Positivkontrolle)."""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent.worker as w
from agent import freetier
from agent.server import app

from .test_freetier import (  # noqa: F401  (Fixture _env wirkt autouse über den Import)
    ANON_A,
    _env,
    _live,
    _rt,
    _run_free,
    _use,
)

E = 1_000_000
IP = "1.1.1.1"
ENV_EXEMPT = "VH_FREE_EXEMPT_KEYS"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _no_exempt(monkeypatch):
    monkeypatch.delenv(ENV_EXEMPT, raising=False)


def _keys():
    return freetier.identity_keys(ANON_A, IP)


def _me(client) -> dict:
    return client.get("/api/me", headers={"x-forwarded-for": IP, "x-anon-id": ANON_A}).json()["free"]


def _drain_pot() -> None:
    freetier.add_pot_ueur(freetier.pot_status()["left_today_ueur"])
    assert freetier.pot_left_ueur() == 0


def _published(ctx, topic: str) -> list[dict]:
    out = []
    for c in ctx.room.local_participant.publish_data.await_args_list:
        if c.kwargs.get("topic") == topic:
            out.append(json.loads(c.kwargs["payload"].decode()))
    return out


def _worker_state(monkeypatch, room: str) -> tuple[dict, object]:
    """Worker auf denselben Zustand starten (ohne Kostenereignis) -> erstes free.state."""
    freetier.register_room(room, "normal", _keys())
    ctx, _ = _run_free(monkeypatch, room=room, humans=1, wait_s=0.1, live_mode=False)
    states = _published(ctx, w.TOPIC_FREE_STATE)
    assert states, "Worker hat keinen free.state publiziert"
    return states[0], ctx


# ----- 1. gleiche Werte: /api/me == Worker == 402 ---------------------------------
SCENARIOS = {
    "frisch": lambda mp: None,
    "angebraucht": lambda mp: _use(ANON_A, ip=IP, eur=0.33),
    "persoenliches_limit": lambda mp: _use(ANON_A, ip=IP, eur=1.0),
    "topf_leer": lambda mp: _drain_pot(),
    "owner": lambda mp: (_use(ANON_A, ip=IP, eur=1.0), _drain_pot(),
                         mp.setenv(ENV_EXEMPT, ",".join(_keys()))),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_api_me_equals_worker_state_and_402(monkeypatch, client, name):
    SCENARIOS[name](monkeypatch)
    me = _me(client)
    st = freetier.free_state(_keys())
    worker, ctx = _worker_state(monkeypatch, f"same-{name}")
    # Anzeige: /api/me und Worker-Topic sind dieselbe Zahl, dasselbe exempt.
    assert me["eur_left"] == worker["eur_left"] == st["eur_left"]
    assert me["eur_per_day"] == worker["eur_per_day"]
    assert bool(me.get("exempt")) == worker["exempt"] == st["exempt"]
    # Entscheidung: Rest > 0 <=> kein 402 <=> Worker beendet nicht wegen Gratis.
    ended = any(c.kwargs.get("reason") == "call_guard:free_limit" for c in ctx.shutdown.call_args_list)
    blocked = _live(client, ANON_A, ip=IP).status_code == 402
    shows_left = me["eur_left"] > 0
    assert shows_left == (not blocked) == (not ended), (name, me, worker, blocked, ended)


def test_new_day_same_values(monkeypatch, client):
    """Neuer UTC-Tag: gestern leer, heute wieder voll, auf beiden Seiten gleich."""
    yesterday = time.time() - 86400
    freetier.add_ueur(_keys(), E, now=yesterday)
    assert freetier.free_state(_keys(), now=yesterday)["left_ueur"] == 0
    me = _me(client)
    worker, ctx = _worker_state(monkeypatch, "same-newday")
    assert me["eur_left"] == worker["eur_left"] == 1.0
    ctx.shutdown.assert_not_called()


def test_reason_values():
    assert freetier.free_state(_keys())["reason"] is None
    _use(ANON_A, ip=IP, eur=1.0)
    assert freetier.free_state(_keys())["reason"] == "personal_limit"


def test_reason_pot_empty():
    _drain_pot()
    st = freetier.free_state(_keys())
    assert st == {**st, "left_ueur": 0, "eur_left": 0.0, "pot_empty": True, "reason": "pot_empty"}


# ----- 2. Owner nie gesperrt, auch im Topf-Pfad ------------------------------------
def test_owner_never_blocked_in_pot_path(monkeypatch, client):
    monkeypatch.setenv(ENV_EXEMPT, _keys()[1])            # nur der IP-Key (wie die Box)
    _use(ANON_A, ip=IP, eur=1.0)
    _drain_pot()
    assert _live(client, ANON_A, ip=IP).status_code == 200                 # kein 402
    st = freetier.free_state(_keys())
    assert st["exempt"] and st["reason"] == "exempt" and st["left_ueur"] > 0
    freetier.register_room("own-pot", "live", _keys())
    used = freetier.used_ueur(_keys())
    ctx, _ = _run_free(monkeypatch, room="own-pot", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)] * 3)
    ctx.shutdown.assert_not_called()                                        # kein free_limit
    assert freetier.used_ueur(_keys()) == used                              # nichts gebucht
    assert freetier.pot_left_ueur() == 0                                    # Topf unberührt
    assert all(s["exempt"] for s in _published(ctx, w.TOPIC_FREE_STATE))


def test_positive_control_non_owner_pot_empty_ends(monkeypatch):
    _drain_pot()
    freetier.register_room("oth-pot", "live", _keys())
    ctx, _ = _run_free(monkeypatch, room="oth-pot", humans=1, wait_s=0.2)
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")


# ----- 3. Worker sagt dem Browser, dass der Call vorbei ist -------------------------
def test_free_limit_publishes_call_end_and_state(monkeypatch):
    monkeypatch.setenv("VH_FREE_EUR_PER_DAY", "0.01")
    freetier.register_room("end-1", "live", _keys())
    ctx, _ = _run_free(monkeypatch, room="end-1", humans=1, wait_s=0.2, metrics=[_rt(1000, 500)])
    ctx.shutdown.assert_called_once_with(reason="call_guard:free_limit")
    assert _published(ctx, w.TOPIC_CALL_END) == [{"reason": "free_limit"}]
    states = _published(ctx, w.TOPIC_FREE_STATE)
    assert states[0]["eur_left"] == 0.01 and states[-1]["eur_left"] == 0.0
    assert states[-1]["reason"] == "personal_limit"


def test_free_state_after_booking_matches_api_me(monkeypatch, client):
    freetier.register_room("book-1", "live", _keys())
    ctx, _ = _run_free(monkeypatch, room="book-1", humans=1, wait_s=0.1, metrics=[_rt(1000, 500)] * 2)
    ctx.shutdown.assert_not_called()
    last = _published(ctx, w.TOPIC_FREE_STATE)[-1]
    assert 0 < last["eur_left"] < 1.0
    assert last["eur_left"] == _me(client)["eur_left"]


# ----- 4. Wächter: keine zweite Rechnung -------------------------------------------
AGENT_DIR = Path(w.__file__).resolve().parent
# Bausteine des Rests: außerhalb von freetier.py verboten (nur free_state benutzen).
FORBIDDEN = {"remaining_ueur", "remaining_eur", "used_ueur", "pot_left_ueur", "limit_ueur", "limit_eur",
             "_pot_read"}
# Wer den Gratis-Rest braucht, muss free_state aufrufen.
MUST_USE = {"server.py", "billing_routes.py", "worker.py"}


def _calls(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "freetier":
            names.add(node.attr)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("freetier"):
            names.update(a.name for a in node.names)
    return names


def test_no_second_free_calculation_outside_freetier():
    offenders = {}
    for path in sorted(AGENT_DIR.rglob("*.py")):
        if "tests" in path.parts or path.name == "freetier.py":
            continue
        bad = _calls(path) & FORBIDDEN
        if bad:
            offenders[str(path.relative_to(AGENT_DIR))] = sorted(bad)
    assert not offenders, f"Gratis-Rest außerhalb von freetier.free_state berechnet: {offenders}"


@pytest.mark.parametrize("name", sorted(MUST_USE))
def test_entry_points_use_free_state(name):
    assert "free_state" in _calls(AGENT_DIR / name)


def test_freetier_wrappers_delegate_to_free_state():
    """remaining_ueur/remaining_eur sind nur Hüllen (keine eigene Formel)."""
    src = (AGENT_DIR / "freetier.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("remaining_ueur", "remaining_eur")):
        called = {c.func.id for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert called == {"free_state"}, (fn.name, called)
