"""Starting the night over from notes.

The store of excuses and the rebuilt conversation are pure enough to test on
their own. The rest runs a real :class:`Game` against a stand-in client that
reports whatever token count the test wants, so the threshold, the journal
rotation and the two transmissions can be checked without a model.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.responses import ResponseFunctionToolCall

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import AssistantMessage
from cabin_fever_x86_core.server import _game as game_module
from cabin_fever_x86_core.server._compaction import (
    COMPACTION_PROMPT,
    EXCUSES_FILE,
    RESUME_DIRECTION,
    STAGE_DIRECTION,
    Excuse,
    draw_excuse,
    load_excuses,
    notes_request,
    rebuilt,
    rewrite,
    rotate,
)
from cabin_fever_x86_core.server._game import MESSAGES_FILE, Game

EXCUSE = Excuse(away="Hang on, the cat.", back="False alarm.")


# --- the excuses on disk --------------------------------------------------


def test_the_shipped_excuses_all_read() -> None:
    excuses = load_excuses()
    assert len(excuses) >= 8
    for excuse in excuses:
        assert excuse.away.strip() and excuse.back.strip()
        # Both halves are spoken aloud, so neither should be a stage direction.
        assert "<" not in excuse.away and "<" not in excuse.back


def test_the_excuses_file_ships_with_the_package() -> None:
    assert EXCUSES_FILE.is_file()
    assert EXCUSES_FILE.parent.name == "server"


def test_a_bad_line_is_skipped_rather_than_fatal(tmp_path: Path) -> None:
    path = tmp_path / "excuses.jsonl"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                json.dumps({"away": "a", "back": "b"}),
                "{not json at all",
                json.dumps({"away": "only half of it"}),
                json.dumps({"away": "c", "back": "d"}),
            ]
        ),
        encoding="utf-8",
    )

    assert load_excuses(path) == [Excuse("a", "b"), Excuse("c", "d")]


def test_a_missing_excuses_file_is_not_fatal(tmp_path: Path) -> None:
    assert load_excuses(tmp_path / "nothing.jsonl") == []
    assert draw_excuse([]) is None


# --- the rebuilt conversation --------------------------------------------


def test_the_notes_are_asked_for_at_the_end_of_the_conversation() -> None:
    history = [{"role": "user", "content": "hello"}]
    asked = notes_request(history)

    assert asked[:-1] == history  # nothing before it is touched
    assert asked[-1]["role"] == "user"
    assert COMPACTION_PROMPT in asked[-1]["content"]
    assert asked[-1]["content"].startswith(f"<{STAGE_DIRECTION}>")


def test_the_conversation_is_rebuilt_in_order() -> None:
    tail = [
        {"type": "reasoning", "encrypted_content": "..."},
        {"type": "function_call", "call_id": "call_1", "name": "type"},
    ]
    conversation = rebuilt("we are up a tree", EXCUSE, tail)

    assert [item.get("role") or item["type"] for item in conversation] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "reasoning",
        "function_call",
    ]
    assert conversation[0]["content"] == EXCUSE.away
    assert "we are up a tree" in conversation[1]["content"]
    assert conversation[2]["content"] == EXCUSE.back
    assert RESUME_DIRECTION in conversation[3]["content"]
    # The reply that tripped the threshold is carried over untouched, so a call
    # it had already decided on still gets its answer.
    assert conversation[4:] == tail


def test_the_notes_are_wrapped_as_a_direction() -> None:
    conversation = rebuilt("notes here", EXCUSE, [])
    notes = conversation[1]["content"]

    assert notes.startswith(f"<{STAGE_DIRECTION}>")
    assert notes.endswith(f"</{STAGE_DIRECTION}>")
    assert "notes here" in notes


def test_without_an_excuse_the_notes_still_land() -> None:
    conversation = rebuilt("notes", None, [{"type": "reasoning"}])

    assert [item.get("role") or item["type"] for item in conversation] == [
        "user",
        "user",
        "reasoning",
    ]


def test_the_direction_tag_matches_the_one_used_all_session() -> None:
    """Notes must not arrive wearing a tag the companion has never seen."""
    assert STAGE_DIRECTION == game_module.STAGE_DIRECTION


# --- the journal ----------------------------------------------------------


def test_the_old_journal_is_moved_aside_and_numbered(tmp_path: Path) -> None:
    journal = tmp_path / MESSAGES_FILE
    journal.write_text("first night\n", encoding="utf-8")

    assert rotate(journal) == tmp_path / "messages.0001.jsonl"
    assert not journal.exists()
    assert (tmp_path / "messages.0001.jsonl").read_text() == "first night\n"

    journal.write_text("second night\n", encoding="utf-8")
    assert rotate(journal) == tmp_path / "messages.0002.jsonl"
    assert (tmp_path / "messages.0001.jsonl").read_text() == "first night\n"


def test_rotating_nothing_is_not_an_error(tmp_path: Path) -> None:
    assert rotate(tmp_path / MESSAGES_FILE) is None


def test_the_journal_is_rewritten_one_item_per_line(tmp_path: Path) -> None:
    journal = tmp_path / MESSAGES_FILE
    journal.write_text("stale\n", encoding="utf-8")
    messages = [{"role": "user", "content": "a"}, {"type": "reasoning", "id": "r1"}]

    rewrite(journal, messages)

    assert [json.loads(line) for line in journal.read_text().splitlines()] == messages


# --- the whole thing, against a stand-in client ---------------------------


@dataclass
class FakeUsage:
    total_tokens: int
    input_tokens: int = 0
    output_tokens: int = 0
    input_tokens_details: Any = None
    output_tokens_details: Any = None


@dataclass
class FakeItem:
    """One item of a reply, in the shape the real ones are stored in."""

    payload: dict[str, Any]

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(self.payload)


@dataclass
class FakeResponse:
    output: list[Any]
    usage: FakeUsage
    output_text: str = ""


@dataclass
class FakeResponses:
    """Hands back queued replies and remembers what it was asked."""

    replies: list[FakeResponse]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self.replies:
            raise AssertionError("the client was asked for more replies than the test queued")
        return self.replies.pop(0)


@dataclass
class FakeClient:
    responses: FakeResponses

    async def close(self) -> None:
        return None


def tool_call(name: str, call_id: str = "call_1", **arguments: Any) -> ResponseFunctionToolCall:
    """A real call object: _handle picks calls out of a reply by their type."""
    return ResponseFunctionToolCall(
        type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments)
    )


def transmit_call(text: str, call_id: str = "call_1") -> ResponseFunctionToolCall:
    return tool_call("transmit", call_id, message=text)


def reasoning() -> FakeItem:
    return FakeItem({"type": "reasoning", "encrypted_content": "shhh", "summary": []})


def test_response_token_usage_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    usage = FakeUsage(
        total_tokens=1800,
        input_tokens=1000,
        output_tokens=800,
        input_tokens_details=SimpleNamespace(cached_tokens=500),
        output_tokens_details=SimpleNamespace(reasoning_tokens=300),
    )

    with caplog.at_level(logging.INFO, logger=game_module.__name__):
        game_module._log_token_usage(SimpleNamespace(usage=usage))

    assert "Input Tokens: 1000 (500 cached); Output Tokens: 800 (300 reasoning)" in caplog.messages


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
def _one_excuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_module, "load_excuses", lambda *_a, **_k: [EXCUSE])


def game_with(
    replies: list[FakeResponse],
    sender: Any,
    monkeypatch: pytest.MonkeyPatch,
    threshold: int = 100,
) -> tuple[Game, FakeResponses]:
    """A game whose model hands back *replies* in order, and the record of asks."""
    client = FakeClient(FakeResponses(list(replies)))
    monkeypatch.setattr(game_module, "create_client", lambda *_: (client, "test-model"))
    return Game(ServerConfig(compaction_threshold=threshold), sender), client.responses


async def test_a_reply_under_the_threshold_changes_nothing(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([reasoning(), transmit_call("evening")], FakeUsage(50))]
    game, _asked = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="say hello"))

        assert spoken == ["evening"]
        assert not list(Path(game._data_dir or ".").glob("messages.0*.jsonl"))


async def test_crossing_the_threshold_writes_the_night_down(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        # Over the threshold, and mid-tool-call.
        FakeResponse([reasoning(), transmit_call("still here")], FakeUsage(120)),
        # The notes.
        FakeResponse([], FakeUsage(30), output_text="Playing zork1. Operator is Sam. Up a tree."),
    ]
    game, responses = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="say hello"))

        assert [call["prompt_cache_key"] for call in responses.calls] == [
            str(game.session_id),
            str(game.session_id),
        ]
        # Both the turn and the notes behind it ask for the long retention: a
        # summary written under the short one would go cold before it was read.
        assert [call["prompt_cache_retention"] for call in responses.calls] == ["24h", "24h"]
        # Excuse, notes, return — and the transmission the reply had decided on.
        assert spoken == [EXCUSE.away, EXCUSE.back, "still here"]

        kinds = [item.get("role") or item["type"] for item in game._messages]
        assert kinds == [
            "assistant",  # the excuse
            "user",  # the notes
            "assistant",  # back at the desk
            "user",  # carry on
            "reasoning",  # the reply that tripped it, kept whole
            "function_call",
            "function_call_output",  # and its answer, appended after
        ]
        assert "Up a tree" in game._messages[1]["content"]


async def test_compact_publicly_writes_down_the_current_conversation(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([], FakeUsage(30), output_text="Operator is Sam. Playing zork1.")]
    game, responses = game_with(replies, sender, monkeypatch)

    async with game:
        game._append({"role": "user", "content": "Let's play zork1."})
        await game.compact()

        assert len(responses.calls) == 1
        assert COMPACTION_PROMPT in responses.calls[0]["input"][-1]["content"]
        assert "Operator is Sam" in game._messages[1]["content"]
        assert Path(game._journal or "").with_name("messages.0001.jsonl").is_file()


async def test_public_compaction_waits_for_an_in_flight_turn(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([], FakeUsage(30), output_text="notes")]
    game, responses = game_with(replies, sender, monkeypatch)
    handling = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handle(_message: Any) -> None:
        handling.set()
        await release.wait()

    async with game:
        game._handle = blocked_handle  # type: ignore[method-assign]
        await game.receive(game_module.UserMessage(content="hello"))
        await handling.wait()

        compacting = asyncio.create_task(game.compact())
        await asyncio.sleep(0)
        assert not compacting.done()
        assert not responses.calls

        release.set()
        await compacting
        assert len(responses.calls) == 1


async def test_shutdown_releases_a_queued_compaction_waiter(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    game, _responses = game_with([], sender, monkeypatch)
    handling = asyncio.Event()

    async def blocked_handle(_message: Any) -> None:
        handling.set()
        await asyncio.Event().wait()

    async with game:
        game._handle = blocked_handle  # type: ignore[method-assign]
        await game.receive(game_module.UserMessage(content="hello"))
        await handling.wait()
        compacting = asyncio.create_task(game.compact())
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closed before compaction completed"):
        await compacting


async def test_shutdown_releases_an_in_flight_compaction_waiter(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    game, _responses = game_with([], sender, monkeypatch)
    compacting_now = asyncio.Event()

    async def blocked_compaction(*_args: Any) -> None:
        compacting_now.set()
        await asyncio.Event().wait()

    async with game:
        game._compact = blocked_compaction  # type: ignore[method-assign]
        compacting = asyncio.create_task(game.compact())
        await compacting_now.wait()

    with pytest.raises(RuntimeError, match="closed before compaction completed"):
        await compacting


async def test_the_notes_are_asked_for_without_the_triggering_reply(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([reasoning(), transmit_call("hello")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="notes"),
    ]
    game, responses = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="hi"))

        asked = responses.calls[1]
        # The direction asking for notes is on the end, and the reply that
        # tripped the threshold is not in what gets summarised.
        assert COMPACTION_PROMPT in asked["input"][-1]["content"]
        assert not any(item.get("type") == "function_call" for item in asked["input"])
        # Tools stay declared but refused: the conversation is full of calls to
        # them, and what is wanted back is prose.
        assert asked["tool_choice"] == "none"
        assert asked["tools"]


async def test_the_old_journal_is_kept_and_the_new_one_written(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([reasoning(), transmit_call("hello")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="notes"),
    ]
    game, _asked = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="hi"))

        journal = Path(game._journal or "")
        aside = journal.with_name("messages.0001.jsonl")
        assert aside.is_file()
        assert "say hello" not in journal.read_text()  # the long night moved out

        written = [json.loads(line) for line in journal.read_text().splitlines()]
        assert written == game._messages  # a resume picks up the compacted one


async def test_notes_that_do_not_come_back_leave_the_night_alone(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([reasoning(), transmit_call("hello")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="   "),  # nothing usable
    ]
    game, _asked = game_with(replies, sender, monkeypatch)
    async with game:
        before = len(game._messages)
        await game._handle(game_module.Interruption(kind="stage_direction", text="hi"))

        # Still said both halves, so the operator is not left hanging.
        assert spoken == [EXCUSE.away, EXCUSE.back, "hello"]
        # And the conversation carried on at full length rather than being lost.
        assert len(game._messages) > before
        assert not list(Path(game._journal or "").parent.glob("messages.0*.jsonl"))


async def test_the_operator_is_not_heard_while_the_night_is_written_down(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [
        FakeResponse([reasoning(), transmit_call("hello")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="notes"),
    ]
    game, _asked = game_with(replies, sender, monkeypatch)
    async with game:
        assert game._afk_seconds_left() == 0
        await game._handle(game_module.Interruption(kind="stage_direction", text="hi"))
        # Away for the summary, and back at the radio the moment it is done.
        assert game._afk_seconds_left() == 0


async def test_the_night_is_written_down_once_per_transmission(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary that is itself over the threshold must not compact forever."""
    replies = [
        FakeResponse([reasoning(), tool_call("read_screen", "c1")], FakeUsage(120)),
        FakeResponse([], FakeUsage(30), output_text="notes"),
        FakeResponse([reasoning(), transmit_call("done")], FakeUsage(500)),
    ]
    game, responses = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="hi"))

        # Three requests: the first reply, the notes, the reply after them. No
        # second set of notes, though the last reply was over the threshold too.
        assert len(responses.calls) == 3
        assert not list(Path(game._journal or "").parent.glob("messages.0002.jsonl"))


async def test_an_empty_transmission_still_reaches_the_client(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([transmit_call("")], FakeUsage(20))]
    game, _responses = game_with(replies, sender, monkeypatch)

    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="say nothing"))

    assert spoken == [""]
    assert game._messages[-1]["output"] == ("Kerchunk. You keyed up the radio without speaking.")


async def test_hint_tool_stays_defined_but_is_allowed_only_when_material_exists(
    sender: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    game, _responses = game_with([], sender, monkeypatch)

    async with game:
        definitions = [tool.definition for tool in game._tools.values()]
        assert any(tool["name"] == "request_hint" for tool in definitions)

        monkeypatch.setattr(game_module, "has_hints", lambda _game: False)
        without_hints = game._allowed_tool_choice(cabin_turn=False)
        assert not any(tool["name"] == "request_hint" for tool in without_hints["tools"])

        monkeypatch.setattr(game_module, "has_hints", lambda _game: True)
        with_hints = game._allowed_tool_choice(cabin_turn=False)
        assert any(tool["name"] == "request_hint" for tool in with_hints["tools"])
        assert definitions == [tool.definition for tool in game._tools.values()]


async def test_an_empty_plain_reply_still_reaches_the_client(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = [FakeResponse([], FakeUsage(20), output_text="   ")]
    game, _responses = game_with(replies, sender, monkeypatch)

    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="say nothing"))

    assert spoken == [""]


async def test_the_extra_model_round_forces_a_transmission(
    sender: Any, spoken: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    long_message = "x" * 500
    replies = [
        *(
            FakeResponse([tool_call("read_screen", f"screen_{model_round}")], FakeUsage(20))
            for model_round in range(game_module.MAX_MODEL_ROUNDS)
        ),
        FakeResponse([transmit_call(long_message, "final")], FakeUsage(20)),
    ]
    game, responses = game_with(replies, sender, monkeypatch)

    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="keep looking"))

    assert spoken == [long_message]
    assert len(responses.calls) == game_module.MAX_MODEL_ROUNDS + 1
    assert all(call["tool_choice"]["type"] == "allowed_tools" for call in responses.calls[:-1])
    assert all(call["parallel_tool_calls"] is False for call in responses.calls)
    assert responses.calls[-1]["tool_choice"] == {"type": "function", "name": "transmit"}
