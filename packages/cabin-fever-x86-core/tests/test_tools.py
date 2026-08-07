"""Behavior of the tools exposed to the companion."""

from types import SimpleNamespace

import pytest

from cabin_fever_x86_core.hints import HintLevel
from cabin_fever_x86_core.server._tools import AfkTool, RequestHintTool


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


def test_request_hint_exposes_only_the_question_and_level() -> None:
    assert set(RequestHintTool.parameters["properties"]) == {"question", "hint_level"}
    assert RequestHintTool.parameters["required"] == ["question", "hint_level"]
    assert RequestHintTool.parameters["properties"]["hint_level"]["enum"] == [
        level.value for level in HintLevel
    ]
    assert "explicitly asks" in RequestHintTool.description
    assert "printed walkthroughs" in RequestHintTool.description


@pytest.mark.parametrize(
    ("requested", "provided"),
    [
        (HintLevel.SMALL_HINT, HintLevel.SMALL_HINT),
        (HintLevel.MEDIUM_HINT, HintLevel.SMALL_HINT),
        (HintLevel.LARGE_HINT, HintLevel.MEDIUM_HINT),
    ],
)
@pytest.mark.asyncio
async def test_request_hint_calibrates_the_level_down(
    monkeypatch: pytest.MonkeyPatch,
    requested: HintLevel,
    provided: HintLevel,
) -> None:
    calls: list[tuple[object, str, str, str, HintLevel]] = []

    async def fake_provide_hint(client, model, game, question, level) -> str:
        calls.append((client, model, game, question, level))
        return "a restrained hint"

    client = object()
    machine = SimpleNamespace(game="zork1")
    monkeypatch.setattr("cabin_fever_x86_core.server._tools.has_hints", lambda _game: True)
    monkeypatch.setattr("cabin_fever_x86_core.server._tools.provide_hint", fake_provide_hint)

    result = await RequestHintTool(client, "test-model", machine).execute(
        {"question": "How do I open the egg?", "hint_level": requested.value}
    )

    assert result.content == "a restrained hint"
    assert calls == [(client, "test-model", "zork1", "How do I open the egg?", provided)]
