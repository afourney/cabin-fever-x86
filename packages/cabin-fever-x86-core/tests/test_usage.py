"""What every request to the model cost, written down beside the conversation.

The ledger itself is pure enough to test on its own. The rest runs a real
:class:`Game` against a stand-in client, so what a turn is charged — and what a
hint or a compaction is charged back to — can be checked without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
    ResponseUsage,
)

from cabin_fever_x86_core import hints as hints_module
from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import AssistantMessage, UserMessage
from cabin_fever_x86_core.server import _game as game_module
from cabin_fever_x86_core.server import _tools as tools_module
from cabin_fever_x86_core.server._game import Game
from cabin_fever_x86_core.server._tools import RequestHintTool
from cabin_fever_x86_core.server._usage import SCHEMA_VERSION, UsageLog, usage_dict
from cabin_fever_x86_core.sessions import USAGE_FILE


@dataclass
class FakeUsage:
    """A breakdown in the shape the API returns one, minus the pydantic."""

    total_tokens: int
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: Any = None
    output_tokens_details: Any = None


@dataclass
class FakeResponse:
    """A reply carrying the metadata the ledger reads off a real one."""

    output: list[Any]
    usage: FakeUsage | None
    output_text: str = ""
    id: str = "resp_test"
    created_at: float = 1787954412.331
    model: str = "gpt-test-5"
    service_tier: str = "default"
    prompt_cache_key: str = "cache-key"
    prompt_cache_retention: str = "24h"
    reasoning: Any = None


@dataclass
class FakeItem:
    """One item of a reply, in the shape the real ones are stored in."""

    payload: dict[str, Any]

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.payload)


@dataclass
class FakeResponses:
    replies: list[FakeResponse]

    async def create(self, **_kwargs: Any) -> FakeResponse:
        if not self.replies:
            raise AssertionError("the client was asked for more replies than the test queued")
        return self.replies.pop(0)


@dataclass
class FakeClient:
    responses: FakeResponses

    async def close(self) -> None:
        return None


def reasoning() -> FakeItem:
    return FakeItem({"type": "reasoning", "encrypted_content": "shhh", "summary": []})


def tool_call(name: str, call_id: str = "call_1", **arguments: Any) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments)
    )


def transmit_call(text: str, call_id: str = "call_1") -> ResponseFunctionToolCall:
    return tool_call("transmit", call_id, message=text)


# --- the ledger on its own ------------------------------------------------


def test_a_record_says_what_was_spent_and_what_served_it(tmp_path: Path) -> None:
    session_id = uuid4()
    turn_id = uuid4()
    parent_turn_id = uuid4()
    log = UsageLog(tmp_path / USAGE_FILE, session_id)

    log.record(
        FakeResponse(
            [],
            FakeUsage(
                total_tokens=41715,
                input_tokens=41203,
                output_tokens=512,
                input_tokens_details=SimpleNamespace(cached_tokens=0, cache_write_tokens=40960),
                output_tokens_details=SimpleNamespace(reasoning_tokens=384),
            ),
            id="resp_5d81b60c4fa927",
            prompt_cache_key="zork1_hint",
            reasoning=SimpleNamespace(effort="medium", context="current_turn"),
        ),
        turn_type="hint",
        turn_id=turn_id,
        parent_turn_id=parent_turn_id,
        call_id="call_Kx9mQ2vTn4",
    )

    (record,) = [json.loads(line) for line in log.path.read_text().splitlines()]
    timestamp = record.pop("timestamp")
    assert timestamp.endswith("+00:00")
    assert record == {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(session_id),
        "turn_type": "hint",
        "turn_id": str(turn_id),
        "parent_turn_id": str(parent_turn_id),
        "call_id": "call_Kx9mQ2vTn4",
        "model_round": 0,
        "response_id": "resp_5d81b60c4fa927",
        "response_created_at": 1787954412.331,
        "model": "gpt-test-5",
        "service_tier": "default",
        "prompt_cache_key": "zork1_hint",
        "prompt_cache_retention": "24h",
        "reasoning": {"effort": "medium", "context": "current_turn"},
        "usage": {
            "input_tokens": 41203,
            "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 40960},
            "output_tokens": 512,
            "output_tokens_details": {"reasoning_tokens": 384},
            "total_tokens": 41715,
        },
    }


def test_a_reply_that_reported_no_usage_is_not_written_down(tmp_path: Path) -> None:
    log = UsageLog(tmp_path / USAGE_FILE, uuid4())

    log.record(FakeResponse([], None), turn_type="player", turn_id=uuid4())

    assert not log.path.exists()


def test_the_breakdown_is_kept_whole_however_the_api_grows_it() -> None:
    usage = ResponseUsage(
        input_tokens=1000,
        input_tokens_details=InputTokensDetails(cached_tokens=768, cache_write_tokens=128),
        output_tokens=200,
        output_tokens_details=OutputTokensDetails(reasoning_tokens=96),
        total_tokens=1200,
    )

    # Every counter the API reported, including the ones no caller asks for by
    # name: what is billed today is not all that will be billed later.
    assert usage_dict(usage) == {
        "input_tokens": 1000,
        "input_tokens_details": {"cached_tokens": 768, "cache_write_tokens": 128},
        "output_tokens": 200,
        "output_tokens_details": {"reasoning_tokens": 96},
        "total_tokens": 1200,
    }


def test_a_ledger_that_cannot_be_written_does_not_end_the_night(tmp_path: Path) -> None:
    # The directory is missing, so the append fails: the cost of one request is
    # worth a warning, not the session.
    log = UsageLog(tmp_path / "gone" / USAGE_FILE, uuid4())

    log.record(FakeResponse([], FakeUsage(10)), turn_type="player", turn_id=uuid4())

    assert not log.path.exists()


# --- what a running game charges where ------------------------------------


@pytest.fixture
def spoken() -> list[str]:
    return []


@pytest.fixture
def sender(spoken: list[str]) -> Any:
    async def send(message: AssistantMessage) -> None:
        spoken.append(message.content)

    return send


@pytest.fixture(autouse=True)
def _own_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _quiet_cabin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_module, "load_interruptions", lambda *_a, **_k: [])


@pytest.fixture(autouse=True)
def _no_excuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_module, "load_excuses", lambda *_a, **_k: [])


def game_with(
    replies: list[FakeResponse],
    sender: Any,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int = 100,
) -> Game:
    """A game whose model hands back *replies* in order."""
    client = FakeClient(FakeResponses(list(replies)))
    monkeypatch.setattr(game_module, "create_client", lambda *_: (client, "test-model"))
    return Game(ServerConfig(compaction_threshold=threshold), sender)


def ledger(game: Game) -> list[dict[str, Any]]:
    """Every row written for this session, in the order it was spent."""
    path = Path(game._data_dir or ".") / USAGE_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_a_player_turn_is_charged_round_by_round(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([reasoning(), tool_call("read_screen")], FakeUsage(20)),
        FakeResponse([transmit_call("evening")], FakeUsage(30)),
    ]
    game = game_with(replies, sender, monkeypatch)
    message = UserMessage(content="hello?")

    async with game:
        await game._handle(message)

        rows = ledger(game)

    assert [row["turn_type"] for row in rows] == ["player", "player"]
    assert [row["turn_id"] for row in rows] == [str(message.id)] * 2
    # One row per round, in the order the rounds happened.
    assert [row["model_round"] for row in rows] == [0, 1]
    assert [row["parent_turn_id"] for row in rows] == [None, None]
    assert [row["call_id"] for row in rows] == [None, None]
    assert [row["usage"]["total_tokens"] for row in rows] == [20, 30]
    assert {row["session_id"] for row in rows} == {str(game.session_id)}


async def test_an_interruption_is_charged_as_the_kind_it_was(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([transmit_call("the kettle")], FakeUsage(20))]
    game = game_with(replies, sender, monkeypatch)
    interruption = game_module.Interruption(kind="stage_direction", text="say hello")

    async with game:
        await game._handle(interruption)

        rows = ledger(game)

    (row,) = rows
    assert row["turn_type"] == "stage_direction"
    assert row["turn_id"] == str(interruption.id)
    assert row["parent_turn_id"] is None


async def test_an_automatic_compaction_is_charged_back_to_the_turn(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        # Over the threshold, so the night gets written down.
        FakeResponse([transmit_call("still here")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="Playing zork1. Up a tree."),
    ]
    game = game_with(replies, sender, monkeypatch)
    message = UserMessage(content="what happened?")

    async with game:
        await game._handle(message)

        rows = ledger(game)

    turn, notes = rows
    assert turn["turn_type"] == "player"
    assert notes["turn_type"] == "compaction"
    # A turn of its own, charged back to the one whose reply tripped it.
    assert notes["turn_id"] != turn["turn_id"]
    assert notes["parent_turn_id"] == str(message.id)
    assert notes["usage"]["total_tokens"] == 30


async def test_a_compaction_the_operator_asked_for_has_no_turn_behind_it(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([], FakeUsage(30), output_text="Playing zork1. Up a tree.")]
    game = game_with(replies, sender, monkeypatch)

    async with game:
        await game.compact()

        rows = ledger(game)

    (row,) = rows
    assert row["turn_type"] == "compaction"
    assert row["parent_turn_id"] is None
    assert UUID(row["turn_id"])


async def test_a_hint_is_charged_to_the_turn_whose_call_asked_for_it(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the hint book is stood in for: the hint itself runs the real
    # provide_hint, so the accounting hook it calls is the one under test.
    monkeypatch.setattr(tools_module, "has_hints", lambda _game: True)
    monkeypatch.setattr(hints_module, "_load_hints", lambda _game: "The canary matters.")

    replies = [
        FakeResponse(
            [
                tool_call(
                    "request_hint", "call_hint_1", question="the egg?", hint_level="SMALL_HINT"
                )
            ],
            FakeUsage(20),
        ),
        # The hint is answered by the same client, between the two rounds.
        FakeResponse(
            [],
            FakeUsage(41715),
            output_text=json.dumps({"status": "answered", "hint": "Listen for a singing bird."}),
            prompt_cache_key="zork1_hint",
        ),
        FakeResponse([transmit_call("try the bird")], FakeUsage(25)),
    ]
    game = game_with(replies, sender, monkeypatch)
    message = UserMessage(content="I am stuck")

    async with game:
        # The shipped hint tool reads the machine for the game in play; this one
        # is already at one, so the call gets as far as the hint itself.
        game._tools[RequestHintTool.name] = RequestHintTool(
            game._client, game._model, SimpleNamespace(game="zork1")
        )
        await game._handle(message)

        rows = ledger(game)

    first, hint, last = rows
    assert [first["turn_type"], hint["turn_type"], last["turn_type"]] == [
        "player",
        "hint",
        "player",
    ]
    # Its own turn, tied back to the exact call in messages.jsonl that spent it.
    assert hint["turn_id"] not in {first["turn_id"], last["turn_id"]}
    assert hint["parent_turn_id"] == str(message.id)
    assert hint["call_id"] == "call_hint_1"
    assert hint["prompt_cache_key"] == "zork1_hint"
    assert hint["usage"]["total_tokens"] == 41715


async def test_the_ledger_outlives_the_conversation_it_paid_for(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([transmit_call("still here")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="Playing zork1. Up a tree."),
        FakeResponse([transmit_call("go on then")], FakeUsage(40)),
    ]
    game = game_with(replies, sender, monkeypatch)

    async with game:
        await game._handle(UserMessage(content="what happened?"))
        # The journal was rotated and rewritten from the notes; the ledger was
        # not, and goes on being appended to.
        await game._handle(UserMessage(content="and then?"))

        rows = ledger(game)
        assert list(Path(game._data_dir or ".").glob("messages.0*.jsonl"))

    assert [row["usage"]["total_tokens"] for row in rows] == [120, 30, 40]
