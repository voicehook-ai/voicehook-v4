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
