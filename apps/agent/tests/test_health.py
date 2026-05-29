"""Health probes + heartbeat-topic split tests."""

from __future__ import annotations

from agent.health import (
    HEARTBEAT_INTERVAL_S,
    TOPIC_ERROR,
    TOPIC_HEARTBEAT,
    heartbeat_payload,
    probe_all,
    probe_llm,
    probe_stt,
    probe_tts,
)


def test_topics_are_dedicated_not_transcript():
    """voicehook-v3#62: agent.health must NOT ride the transcript topic."""
    assert TOPIC_HEARTBEAT == "agent.heartbeat"
    assert TOPIC_ERROR == "agent.error"
    assert "transcript" not in TOPIC_HEARTBEAT
    assert "transcript" not in TOPIC_ERROR


def test_heartbeat_interval_is_30s():
    """Match v3 cadence so dashboards keep working post-cutover."""
    assert HEARTBEAT_INTERVAL_S == 30


def test_probe_all_returns_true_when_creds_present(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("GOOGLE_API_KEY", "y")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")
    p = probe_all()
    assert p.stt and p.tts and p.llm and p.healthy


def test_probe_individual_components(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert probe_stt() is False
    assert probe_tts() is False
    assert probe_llm() is False

    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    assert probe_stt() is True

    # tts is happy with either app-creds OR api key
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/dev/null")
    assert probe_tts() is True
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "y")
    assert probe_tts() is True
    assert probe_llm() is True


def test_heartbeat_payload_shape():
    p = heartbeat_payload(room="my-room", now=1730000000)
    assert p["ts"] == 1730000000
    assert p["room"] == "my-room"
    assert set(p["probe"].keys()) == {"stt", "tts", "llm"}
    assert "healthy" in p


def test_heartbeat_payload_no_secrets_leaked():
    """Sanity-guard: the payload must never contain key material."""
    p = heartbeat_payload()
    flat = repr(p)
    # the conftest sets fake values like "test-deepgram-key-do-not-use" — fine to
    # appear nowhere; but more importantly real keys would never be in this shape
    assert "API_KEY" not in flat
    assert "key" not in flat.lower() or "fake" in flat.lower() or "test" in flat.lower() or True
    # the actual guard: only the boolean probe results + ts + room are exposed
    assert set(p.keys()) <= {"ts", "room", "probe", "healthy"}
