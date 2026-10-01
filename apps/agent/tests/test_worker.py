"""Unit tests for the LiveKit worker skeleton (PR-3).

We don't spin up a real LiveKit server in CI — these tests verify the
worker's shape: entrypoint signature, WorkerOptions construction, agent name
defaulting + override via env.
"""

from __future__ import annotations

import inspect

from livekit.agents import WorkerOptions

from agent.worker import AGENT_NAME, build_worker_options, entrypoint


def test_entrypoint_is_async_with_single_ctx_arg():
    sig = inspect.signature(entrypoint)
    assert inspect.iscoroutinefunction(entrypoint)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "ctx"


def test_worker_options_uses_voice_ai_name_by_default():
    assert AGENT_NAME == "voice-ai"
    opts = build_worker_options()
    assert isinstance(opts, WorkerOptions)
    assert opts.agent_name == "voice-ai"
    assert opts.entrypoint_fnc is entrypoint


def test_worker_options_respects_env_override(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_AGENT_NAME", "test-agent-xyz")
    # AGENT_NAME is captured at import-time; re-import the module to pick it up
    import importlib

    import agent.worker as worker_mod

    importlib.reload(worker_mod)
    try:
        assert worker_mod.AGENT_NAME == "test-agent-xyz"
        assert worker_mod.build_worker_options().agent_name == "test-agent-xyz"
    finally:
        monkeypatch.delenv("VOICEHOOK_AGENT_NAME", raising=False)
        importlib.reload(worker_mod)


def test_entrypoint_subscribes_to_audio():
    """Regression guard (#55): the agent MUST connect with explicit audio
    auto-subscribe, else STT silently gets no input (recurring post-deploy bug)."""
    import inspect as _inspect
    src = _inspect.getsource(entrypoint)
    assert "auto_subscribe=AutoSubscribe.AUDIO_ONLY" in src, \
        "ctx.connect must use AutoSubscribe.AUDIO_ONLY so STT receives audio"


# ----- Pipeline-Auswahl: normal (Default) vs. Gemini-Live-Testworker ---------
def test_build_session_default_is_classic_stt_llm_tts(monkeypatch):
    import agent.worker as w
    monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
    seen = {}
    monkeypatch.setattr(w, "AgentSession", lambda **kw: seen.update(kw) or "S")
    monkeypatch.setattr(w, "build_stt", lambda: "STT")
    monkeypatch.setattr(w, "build_tts", lambda: "TTS")
    monkeypatch.setattr(w, "build_llm", lambda: "LLM")
    assert w.build_session() == "S"
    assert seen == {"stt": "STT", "tts": "TTS", "llm": "LLM"}
    assert w.is_live() is False


def test_build_session_live_uses_realtime_model_only(monkeypatch):
    import agent.live as live
    import agent.worker as w
    monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
    seen = {}
    monkeypatch.setattr(w, "AgentSession", lambda **kw: seen.update(kw) or "S")
    monkeypatch.setattr(live, "build_live_llm", lambda: "REALTIME")
    assert w.build_session() == "S"
    assert seen == {"llm": "REALTIME"}
    assert w.is_live() is True


def test_live_worker_refuses_room_when_month_budget_used_up(monkeypatch):
    """Budget weg -> keine Live-Session wird gebaut, der Job endet sofort."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import agent.worker as w
    from agent import budget

    monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
    monkeypatch.setenv("VOICEHOOK_LIVE_BUDGET_USD_MONTH", "1")
    budget.add_usd(1.0)
    built = []
    monkeypatch.setattr(w, "build_session", lambda: built.append(1))
    ctx = SimpleNamespace(
        connect=AsyncMock(),
        room=SimpleNamespace(name="r1", local_participant=SimpleNamespace(identity="a")),
        job=SimpleNamespace(id="j1"),
        shutdown=MagicMock(),
    )
    asyncio.run(w.entrypoint(ctx))
    assert built == []
    ctx.shutdown.assert_called_once_with(reason="live_budget_exhausted")


def test_live_worker_books_cost_and_ends_call_at_budget():
    import inspect as _inspect

    import agent.worker as w

    src = _inspect.getsource(w.entrypoint)
    assert "budget.add_usd(usd)" in src
    assert '"live_budget"' in src


# ----- Kostenmeldung: nur bei geänderter Summe, mit Basis; Budget unverändert ----
class _Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, fn=None):
        if fn is None:
            return lambda f: self.on(event, f)
        self.handlers.setdefault(event, []).append(fn)
        return fn

    def emit(self, event, ev):
        for fn in self.handlers.get(event, []):
            fn(ev)


def _run_entrypoint_with_metrics(monkeypatch, *, live_mode, metrics, exempt=True):
    """Treibt entrypoint mit Fake-Raum/Session und liefert die gesendeten cost-Payloads."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import agent.worker as w

    if live_mode:
        monkeypatch.setenv("VOICEHOOK_PIPELINE", "live")
    else:
        monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
        monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
        from agent import freetier
        if exempt:
            freetier.register_room("r1", "normal", [], exempt=True)  # fail-closed seit PR #93
        else:  # Gratis-Raum eines Kunden: gezählt, nicht exempt
            freetier.register_room("r1", "normal", freetier.identity_keys("anon-cost-0001", "7.7.7.7"))
    session = _Emitter()
    session.start = AsyncMock()
    session.aclose = AsyncMock()
    monkeypatch.setattr(w, "build_session", lambda: session)
    room = _Emitter()
    room.name = "r1"
    room.remote_participants = {}
    room.local_participant = SimpleNamespace(identity="voice-ai", publish_data=AsyncMock())
    ctx = SimpleNamespace(connect=AsyncMock(), room=room, job=SimpleNamespace(id="j1"),
                          shutdown=MagicMock(), delete_room=AsyncMock())

    async def _go():
        await w.entrypoint(ctx)
        for m in metrics:
            session.emit("metrics_collected", SimpleNamespace(metrics=m))
            await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    return [json.loads(c.kwargs["payload"]) for c in room.local_participant.publish_data.call_args_list
            if c.kwargs.get("topic") == "cost"]


def _metric(kind, **kw):
    return type(kind, (), kw)()


_COST_METRICS = [
    _metric("TTSMetrics", characters_count=100),
    _metric("VADMetrics"),                              # kein Betrag -> keine Meldung
    _metric("TTSMetrics", characters_count=0),          # Betrag 0 -> keine Meldung
    _metric("STTMetrics", audio_duration=60.0),
]


def _eur_normal(*usds):
    from agent.billing import pricing
    return round(sum(pricing.charge_ueur(u, "normal") for u in usds) / 1e6, 4)


def test_cost_topic_customer_room_sends_euro_without_usd(monkeypatch):
    """Oliver 01.10.: Kundenpreis in EUR (Faktor + MwSt); Rohkosten nie im Kundenraum,
    sonst wäre die Marge ablesbar."""
    sent = _run_entrypoint_with_metrics(monkeypatch, live_mode=False, metrics=_COST_METRICS, exempt=False)
    assert len(sent) == 2                                   # Positivkontrolle: echte Änderungen kommen an
    assert sent[0] == {"eur": _eur_normal(0.003), "mode": "pipeline"}
    assert sent[0]["eur"] == 0.0094                         # 0,003 USD x 0,8807 x 3 x 1,19
    assert sent[1] == {"eur": _eur_normal(0.003, 0.0077), "mode": "pipeline"}
    assert all("usd" not in p and "basis" not in p for p in sent)


def test_cost_topic_admin_room_adds_raw_usd_and_basis(monkeypatch):
    sent = _run_entrypoint_with_metrics(monkeypatch, live_mode=False, metrics=_COST_METRICS)
    assert len(sent) == 2
    assert sent[0] == {"eur": _eur_normal(0.003), "mode": "pipeline", "usd": 0.003,
                       "basis": {"tts_chars": 100}, "prices_as_of": "2026-09-30"}
    assert sent[1]["usd"] == 0.0107
    assert sent[1]["basis"] == {"tts_chars": 100, "stt_audio_s": 60.0}


def test_cost_topic_silent_without_metrics(monkeypatch):
    assert _run_entrypoint_with_metrics(monkeypatch, live_mode=False, metrics=[]) == []


def test_live_cost_still_booked_into_month_budget(monkeypatch):
    from agent import budget, freetier

    freetier.register_room("r1", "live", [], exempt=True)  # Demo-/Admin-Raum (Live-Worker ist fail-closed)
    rt = _metric("RealtimeModelMetrics", input_tokens=1000, output_tokens=500,
                 input_token_details=None, output_token_details=None)
    sent = _run_entrypoint_with_metrics(monkeypatch, live_mode=True, metrics=[rt])
    expected = (1000 * 3.00 + 500 * 12.00) / 1e6
    assert budget.spent_usd() == __import__("pytest").approx(expected)
    from agent.billing import pricing
    assert sent == [{"eur": round(pricing.charge_ueur(expected, "live") / 1e6, 4), "mode": "live",
                     "usd": round(expected, 5),
                     "basis": {"rt_audio_in_tokens": 1000, "rt_audio_out_tokens": 500,
                               "rt_text_in_tokens": 0, "rt_text_out_tokens": 0},
                     "prices_as_of": "2026-09-30"}]


# ----- UI-Paket 6: Operator-Text ab Sprechbeginn (transcript.live) ---------------
def test_transcript_live_start_and_end_for_operator_speech(monkeypatch):
    """Sprechbeginn einer Operator-Ausgabe -> transcript.live phase=start mit vollem
    Text; Ende -> phase=end. Eigenantworten senden nichts; `transcript` bleibt wie es war."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import agent.worker as w

    monkeypatch.delenv("VOICEHOOK_PIPELINE", raising=False)
    monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
    from agent import freetier
    freetier.register_room("r1", "normal", [], exempt=True)
    session = _Emitter()
    session.start = AsyncMock()
    session.aclose = AsyncMock()
    session.current_speech = None
    cbs = []
    op = SimpleNamespace(id="speech_op1", interrupted=False, done=lambda: False,
                         add_done_callback=lambda f: cbs.append(f))
    own = SimpleNamespace(id="speech_own", interrupted=False, done=lambda: False,
                          add_done_callback=lambda f: cbs.append(f))
    session.say = MagicMock(return_value=op)
    session.interrupt = MagicMock()
    monkeypatch.setattr(w, "build_session", lambda: session)
    got = {}
    real = w.build_relay_handlers

    def _cap(*a, **kw):
        got["h"] = real(*a, **kw)
        return got["h"]
    monkeypatch.setattr(w, "build_relay_handlers", _cap)
    room = _Emitter()
    room.name = "r1"
    room.remote_participants = {}
    room.local_participant = SimpleNamespace(identity="voice-ai", publish_data=AsyncMock())
    ctx = SimpleNamespace(connect=AsyncMock(), room=room, job=SimpleNamespace(id="j1"),
                          shutdown=MagicMock(), delete_room=AsyncMock())

    @dataclass_pkt
    class _P:
        topic: str
        data: bytes

    async def _go():
        await w.entrypoint(ctx)
        await got["h"].on_say(_P("operator.say", json.dumps({"text": "Hallo Oliver, hier ist der Plan."}).encode()))
        session.current_speech = own                       # Eigenantwort: kein transcript.live
        session.emit("agent_state_changed", SimpleNamespace(old_state="thinking", new_state="speaking"))
        session.current_speech = op
        session.emit("agent_state_changed", SimpleNamespace(old_state="listening", new_state="speaking"))
        session.emit("agent_state_changed", SimpleNamespace(old_state="listening", new_state="speaking"))  # kein Doppel
        await asyncio.sleep(0)
        op.interrupted = True
        for f in list(cbs):
            f(op)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    live = [json.loads(c.kwargs["payload"]) for c in room.local_participant.publish_data.call_args_list
            if c.kwargs.get("topic") == "transcript.live"]
    assert live == [
        {"phase": "start", "role": "operator", "id": "speech_op1", "text": "Hallo Oliver, hier ist der Plan."},
        {"phase": "end", "role": "operator", "id": "speech_op1", "interrupted": True},
    ]
    tr = [c for c in room.local_participant.publish_data.call_args_list if c.kwargs.get("topic") == "transcript"]
    assert tr == []                                        # Echo-Topic unberührt (kommt erst nach dem Sprechen)


from dataclasses import dataclass as dataclass_pkt  # noqa: E402
