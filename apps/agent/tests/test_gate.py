"""SpeechGate: in der Stille geht kein Audio zur STT, Sprache mit Vor- und Nachlauf."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from livekit.agents.vad import VADEventType

from agent.gate import SpeechGate, gate_enabled

FRAME_S = 0.1


def _frame(i: int, speech: bool):
    return SimpleNamespace(i=i, speech=speech, sample_rate=16000, samples_per_channel=int(16000 * FRAME_S))


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class FakeVADStream:
    """Meldet START beim ersten Sprach-Frame, END beim ersten Stille-Frame danach."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.speaking = False

    def push_frame(self, f) -> None:  # noqa: ANN001
        if f.speech and not self.speaking:
            self.speaking = True
            self.q.put_nowait(SimpleNamespace(type=VADEventType.START_OF_SPEECH))
        elif not f.speech and self.speaking:
            self.speaking = False
            self.q.put_nowait(SimpleNamespace(type=VADEventType.END_OF_SPEECH))

    def end_input(self) -> None:
        self.q.put_nowait(None)

    async def aclose(self) -> None:
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        ev = await self.q.get()
        if ev is None:
            raise StopAsyncIteration
        return ev


class FakeVAD:
    def stream(self) -> FakeVADStream:
        return FakeVADStream()


def _run(pattern: str, **kw) -> tuple[list[int], SpeechGate]:
    clock = FakeClock()
    gate = SpeechGate(FakeVAD(), clock=clock, **kw)

    async def src():
        for i, c in enumerate(pattern):
            clock.t = i * FRAME_S
            yield _frame(i, c == "S")

    async def main():
        return [f.i async for f in gate.filter(src())]

    return asyncio.run(main()), gate


def test_pure_silence_sends_nothing():
    out, gate = _run("." * 100)
    assert out == []
    assert gate.passed_s == 0.0
    assert gate.dropped_s > 9.0


def test_speech_passes_with_preroll_and_hangover():
    # 20 Stille, 5 Sprache, 30 Stille; Vorlauf 0,3 s = 3 Frames, Nachlauf 1,0 s
    out, _ = _run("." * 20 + "S" * 5 + "." * 30, preroll_s=0.3, hangover_s=1.0)
    assert out[0] == 17  # Vorlauf: 3 Frames vor dem ersten Sprach-Frame
    assert set(range(20, 25)) <= set(out)  # alle Sprach-Frames
    assert 25 + 9 in out  # Nachlauf läuft noch knapp 1 s
    assert 25 + 12 not in out  # danach wieder zu
    assert out == sorted(out)  # Reihenfolge bleibt


def test_positive_control_without_gate_everything_would_pass():
    # Kontrolle: bei durchgehender Sprache geht alles durch (Gate blockiert nicht blind)
    out, gate = _run("S" * 50)
    assert out == list(range(50))
    assert gate.dropped_s == 0.0


def test_gate_default_on_and_switchable(monkeypatch):
    monkeypatch.delenv("VOICEHOOK_STT_GATE", raising=False)
    assert gate_enabled()
    monkeypatch.setenv("VOICEHOOK_STT_GATE", "0")
    assert not gate_enabled()


def test_relay_agent_routes_audio_through_gate(monkeypatch):
    from livekit.agents import Agent

    from agent.relay import RelayAgent

    seen = {}
    monkeypatch.setattr(Agent.default, "stt_node", lambda self, audio, ms: seen.setdefault("audio", audio))
    gate = SimpleNamespace(filter=lambda a: ("gated", a))
    RelayAgent(instructions="x", gate=gate).stt_node("RAW", None)
    assert seen["audio"] == ("gated", "RAW")
    seen.clear()
    RelayAgent(instructions="x").stt_node("RAW", None)
    assert seen["audio"] == "RAW"
