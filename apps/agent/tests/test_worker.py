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
