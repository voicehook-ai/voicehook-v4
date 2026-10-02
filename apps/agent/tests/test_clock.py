"""Delta kennt Datum, Wochentag und Uhrzeit (Europe/Berlin), Oliver 02.10.2026.

Eingefrorene Uhr. Positivkontrolle: auf d51d939 (vor diesem PR) sind alle Tests rot
(agent.clock fehlt, RelayAgent kennt `now=` nicht, Status-Turn ohne Zeit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from livekit.agents import Agent, llm

from agent import clock, live
from agent.core import core_live, core_normal
from agent.relay import CLOCK_ID, RelayAgent, build_relay_handlers

# Fr 02.10.2026 12:05 UTC = 14:05 MESZ; Sa 31.01.2026 23:30 UTC = So 00:30 MEZ.
SUMMER = datetime(2026, 10, 2, 12, 5, tzinfo=UTC)
WINTER = datetime(2026, 1, 31, 23, 30, tzinfo=UTC)
SUMMER_TXT = "Freitag, 2. Oktober 2026, 14:05 Uhr"
WINTER_TXT = "Sonntag, 1. Februar 2026, 00:30 Uhr"


def test_now_block_berlin_summer_and_winter():
    assert SUMMER_TXT in clock.now_block(SUMMER)
    assert WINTER_TXT in clock.now_block(WINTER)       # Datumswechsel durch die Zeitzone
    assert "Europe/Berlin" in clock.now_block(SUMMER)
    # Positivkontrolle: UTC-Wert stünde falsch da
    assert "12:05" not in clock.now_block(SUMMER)


@pytest.mark.parametrize("utc,hour", [
    (datetime(2026, 3, 29, 0, 59, tzinfo=UTC), 1),     # vor Umstellung: MEZ
    (datetime(2026, 3, 29, 1, 0, tzinfo=UTC), 3),      # danach: MESZ
    (datetime(2026, 10, 25, 0, 59, tzinfo=UTC), 2),    # vor Rückstellung: MESZ
    (datetime(2026, 10, 25, 1, 0, tzinfo=UTC), 2),     # danach: MEZ (02:00)
    (datetime(2026, 7, 1, 10, 0, tzinfo=UTC), 12),
])
def test_fallback_matches_zoneinfo(utc, hour):
    assert clock._berlin_fallback(utc).hour == hour
    assert clock.berlin(utc).hour == hour


def _ctx() -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content="KERN")
    ctx.add_message(role="user", content="Welcher Tag ist heute?")
    return ctx


@pytest.mark.asyncio
async def test_normal_every_answer_gets_fresh_time(monkeypatch):
    seen: list[list] = []

    async def fake_llm_node(_agent, ctx, _tools, _ms):
        seen.append(list(ctx.items))
        yield "ok"

    monkeypatch.setattr(Agent.default, "llm_node", fake_llm_node)
    t = {"now": SUMMER}
    agent = RelayAgent(instructions=core_normal(), speakers=None, now=lambda: t["now"])
    [c async for c in agent.llm_node(_ctx(), [], None)]
    t["now"] = WINTER
    [c async for c in agent.llm_node(_ctx(), [], None)]

    for items, want in ((seen[0], SUMMER_TXT), (seen[1], WINTER_TXT)):
        ids = [i.id for i in items]
        assert ids.count(CLOCK_ID) == 1                  # nie gestapelt
        k = ids.index(CLOCK_ID)
        assert items[k].role == "system" and want in items[k].text_content
        assert all(i.role == "system" for i in items[:k])  # hinter den Instruktionen
        assert items[-1].role == "user"                   # Gespräch unverändert dahinter


def test_core_stays_fixed():
    """Der Kern enthält keine Zeit; sie ist eine eigene Schicht."""
    assert "Uhrzeit" not in core_normal() and "Uhrzeit" not in core_live()


def test_live_start_instruction_has_time_after_core():
    t = live.live_base_instructions(SUMMER)
    assert t.startswith(live.LIVE_CORE_INSTRUCTIONS)
    assert SUMMER_TXT in t and t.index(SUMMER_TXT) > len(live.LIVE_CORE_INSTRUCTIONS)


@pytest.mark.asyncio
async def test_live_status_turn_carries_current_time():
    agent = MagicMock()
    agent.chat_ctx = llm.ChatContext.empty()

    async def _upd(ctx):
        agent.chat_ctx = ctx

    agent.update_chat_ctx = AsyncMock(side_effect=_upd)
    agent.update_instructions = AsyncMock()
    t = {"now": SUMMER}
    agent.now = lambda: t["now"]
    h = build_relay_handlers(MagicMock(), agent, live=True, status_interval_s=0.0)

    class _Pkt:
        def __init__(self, d: bytes) -> None:
            self.data = d

    await h.on_status(_Pkt(b'{"doing": "baut den Fix"}'))
    first = agent.chat_ctx.items[-1].text_content
    assert first.startswith("[System]") and SUMMER_TXT in first
    t["now"] = WINTER
    await h.on_status(_Pkt(b'{"doing": "testet"}'))
    turns = [i.text_content for i in agent.chat_ctx.items]
    assert len(turns) == 1 and WINTER_TXT in turns[0]   # alter Status-Turn ersetzt
    agent.update_instructions.assert_not_awaited()
