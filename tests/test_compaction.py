"""Starting the night over from notes.

The store of excuses and the rebuilt conversation are pure enough to test on
their own. The rest runs a real :class:`Game` against a stand-in client that
reports whatever token count the test wants, so the threshold, the journal
rotation and the two transmissions can be checked without a model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from openai.types.responses import ResponseFunctionToolCall

from cabin_fever_x86.config import ServerConfig
from cabin_fever_x86.messages import AssistantMessage
from cabin_fever_x86.server import _game as game_module
from cabin_fever_x86.server._compaction import (
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
from cabin_fever_x86.server._game import MESSAGES_FILE, Game

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
    game, _asked = game_with(replies, sender, monkeypatch)
    async with game:
        await game._handle(game_module.Interruption(kind="stage_direction", text="say hello"))

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
