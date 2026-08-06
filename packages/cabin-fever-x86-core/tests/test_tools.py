"""Behavior of the tools exposed to the companion."""

import pytest

from cabin_fever_x86_core.server._tools import AfkTool


@pytest.mark.asyncio
async def test_afk_ends_the_turn_after_returning(monkeypatch) -> None:
    transmitted: list[str] = []
    afk_delays: list[float] = []

    async def transmit(message: str) -> None:
        transmitted.append(message)

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr("cabin_fever_x86_core.server._tools.asyncio.sleep", no_wait)
    tool = AfkTool(transmit, afk_delays.append)

    result = await tool.execute(
        {
            "leaving_message": "Putting another log on.",
            "returning_message": "All right, I am back.",
            "delay": 20,
        }
    )

    assert transmitted == ["Putting another log on.", "All right, I am back."]
    assert afk_delays == [20]
    assert result.end_turn is True
