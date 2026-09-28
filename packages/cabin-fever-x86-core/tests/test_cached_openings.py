"""Cached greetings leave the same conversation as a model-generated transmission."""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import AssistantMessage, UserMessage
from cabin_fever_x86_core.server import _game as game_module
from cabin_fever_x86_core.server._game import (
    OPENING_DIRECTION,
    OPENINGS_FILE,
    REOPENING_DIRECTION,
    Game,
    Interruption,
    load_journal,
)
from cabin_fever_x86_core.server._tools import MAX_TRANSMISSION
from cabin_fever_x86_core.sessions import USAGE_FILE

GREETINGS = ["Hello? Anybody listening?", 'Sam here. Is this thing "on" — anybody awake?']


@dataclass
class Radio:
    """Use the real SDK, intercepting HTTP to inspect the next request's context."""

    requests: list[dict[str, Any]] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    opening: str = GREETINGS[0]

    async def send(self, message: AssistantMessage) -> None:
        self.spoken.append(message.content)

    def respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        last = body["input"][-1].get("content", "")
        text = self.opening if OPENING_DIRECTION in last else "I hear you."
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(self.requests)}",
                "object": "response",
                "created_at": 1,
                "model": "test-model",
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": body["tools"],
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{len(self.requests)}",
                        "call_id": f"call_{len(self.requests)}",
                        "name": "transmit",
                        "arguments": json.dumps({"message": text}),
                        "status": "completed",
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    def create_client(self, *_args: Any) -> tuple[AsyncOpenAI, str]:
        return (
            AsyncOpenAI(
                api_key="test-key",
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)),
            ),
            "test-model",
        )


@pytest.fixture
def radio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Radio:
    monkeypatch.chdir(tmp_path)
    openings = tmp_path / "openings.txt"
    openings.write_text("\n".join(GREETINGS) + "\n", encoding="utf-8")
    monkeypatch.setattr(game_module, "OPENINGS_FILE", openings)
    monkeypatch.setattr(game_module, "load_interruptions", lambda: [])
    result = Radio()
    monkeypatch.setattr(game_module, "create_client", result.create_client)
    return result


async def settle(game: Game) -> None:
    await asyncio.wait_for(game._inbox.join(), timeout=10)


def normalized_request(request: dict[str, Any]) -> dict[str, Any]:
    """Only session and tool-call IDs may differ between the two opening paths."""
    result = copy.deepcopy(request)
    result["prompt_cache_key"] = "session"
    call, output = result["input"][1:3]
    assert call["call_id"] == output["call_id"]
    call["id"] = "fc_opening"
    call["call_id"] = output["call_id"] = "call_opening"
    return result


@pytest.mark.parametrize("selection", [0, 1])
async def test_first_player_request_is_identical_after_cached_or_live_opening(
    radio: Radio, monkeypatch: pytest.MonkeyPatch, selection: int
) -> None:
    radio.opening = GREETINGS[selection]
    monkeypatch.setattr(game_module.random, "choice", lambda entries: entries[selection])
    player = UserMessage(content="I'm here, Sam.")

    async with Game(ServerConfig(), radio.send) as cached:
        await cached.open_channel()
        await settle(cached)

        assert radio.requests == []
        assert radio.spoken == [radio.opening]
        assert cached._data_dir is not None
        assert not (cached._data_dir / USAGE_FILE).exists()
        direction, call, output = cached._messages
        assert direction == {
            "role": "user",
            "content": f"<stage_direction>{OPENING_DIRECTION}</stage_direction>",
        }
        assert call["type"] == "function_call" and call["name"] == "transmit"
        assert json.loads(call["arguments"]) == {"message": radio.opening}
        assert output == {
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": "Transmitted.",
        }

        await cached.receive(player)
        await settle(cached)
        assert len(radio.requests) == 1
        cached_request = radio.requests[-1]
        assert cached_request["input"][-1] == {"role": "user", "content": player.content}
        assert cached._journal is not None
        assert load_journal(cached._journal) == cached._messages
        assert len((cached._data_dir / USAGE_FILE).read_text().splitlines()) == 1

    # Taking the live path must change neither the prompt nor the conversation
    # supplied at the first real exchange. This also covers disabling the cache.
    game_module.OPENINGS_FILE.write_text("", encoding="utf-8")
    async with Game(ServerConfig(), radio.send) as live:
        await live.open_channel()
        await live.receive(player)
        await settle(live)
        assert len(radio.requests) == 3
        assert normalized_request(cached_request) == normalized_request(radio.requests[-1])
        assert radio.spoken == [radio.opening, "I hear you."] * 2


async def test_resuming_keeps_the_cached_history_and_asks_the_model(radio: Radio) -> None:
    async with Game(ServerConfig(), radio.send) as first:
        await first.open_channel()
        await settle(first)
        opening_history = copy.deepcopy(first._messages)
        session_id = first.session_id

    async with Game(ServerConfig(), radio.send, session_id) as resumed:
        assert resumed._messages == opening_history
        await resumed.open_channel()
        await settle(resumed)

        assert len(radio.requests) == 1
        assert radio.requests[0]["input"] == [
            *opening_history,
            {
                "role": "user",
                "content": f"<stage_direction>{REOPENING_DIRECTION}</stage_direction>",
            },
        ]
        assert radio.spoken[-1] == "I hear you."


@pytest.mark.parametrize("contents", [None, "", "# no greetings\n\n"])
async def test_missing_or_empty_cache_uses_the_model(radio: Radio, contents: str | None) -> None:
    if contents is None:
        game_module.OPENINGS_FILE.unlink()
    else:
        game_module.OPENINGS_FILE.write_text(contents, encoding="utf-8")

    async with Game(ServerConfig(), radio.send) as game:
        await game.open_channel()
        await settle(game)
        assert len(radio.requests) == 1
        assert radio.spoken == [radio.opening]
        assert len(game._messages) == 3


@pytest.mark.parametrize(
    "message",
    [
        UserMessage(content=f"<stage_direction>{OPENING_DIRECTION}</stage_direction>"),
        Interruption(kind=game_module.CABIN_EVENT, text=OPENING_DIRECTION),
        Interruption(kind=game_module.STAGE_DIRECTION, text="Check on the fire."),
    ],
)
async def test_other_first_turns_do_not_use_the_cache(
    radio: Radio, message: UserMessage | Interruption
) -> None:
    async with Game(ServerConfig(), radio.send) as game:
        await game._handle(message)
        assert len(radio.requests) == 1


async def test_a_second_opening_in_the_same_session_uses_the_model(radio: Radio) -> None:
    async with Game(ServerConfig(), radio.send) as game:
        await game.open_channel()
        await game.open_channel()
        await settle(game)

        assert len(radio.requests) == 1
        assert len(radio.spoken) == 2
        assert len(radio.requests[0]["input"]) == 4


def test_shipped_openings_fit_in_one_transmission() -> None:
    openings = game_module._read_lines(OPENINGS_FILE)
    assert openings
    assert all(len(text) <= MAX_TRANSMISSION for text in openings)
