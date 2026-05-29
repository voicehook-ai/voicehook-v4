"""LLM factory tests — verify Gemini Flash default + env override."""

from __future__ import annotations

from agent.llm import DEFAULT_MODEL, build_llm


def test_default_model_is_gemini_flash():
    assert DEFAULT_MODEL == "gemini-2.5-flash"


def test_build_llm_returns_google_llm_with_default_model():
    llm = build_llm()
    cls = type(llm)
    assert cls.__module__.startswith("livekit.plugins.google")
    assert cls.__name__ == "LLM"
    # introspect via _opts (matches the voice.py pattern)
    assert llm._opts.model == "gemini-2.5-flash"


def test_env_override(monkeypatch):
    monkeypatch.setenv("VOICEHOOK_LLM_MODEL", "gemini-1.5-flash")
    llm = build_llm()
    assert llm._opts.model == "gemini-1.5-flash"
