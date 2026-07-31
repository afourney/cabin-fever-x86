"""Reopening a session puts back both halves of the night.

The conversation comes back from ``messages.jsonl`` and the game from the
autosave, and neither is much use without the other: a companion that remembers
the evening but faces an empty machine will talk about a game that is not
running, and one that has the game but not the evening will greet the operator
as a stranger.

No model is involved. The AI client is replaced with one that raises if anything
reaches for it, since none of this should ever reach a request. Only the tests
that start a game need a ROM on the disk; the ones about the conversation run
anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from cabin_fever_x86.config import ServerConfig
from cabin_fever_x86.messages import AssistantMessage
from cabin_fever_x86.server import _game as game_module
from cabin_fever_x86.server._game import (
    INTERRUPTED_TOOL_OUTPUT,
    OPENING_DIRECTION,
    REOPENING_DIRECTION,
    Game,
    load_journal,
    resolve_interrupted_calls,
)
from cabin_fever_x86.server._machine import GAMES_DIR

ROM = "zork1.z5"


@pytest.fixture(autouse=True)
def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the AI client. Touching it is a test bug, not a pass."""

    class Unusable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the model was reached for {name!r}")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(game_module, "create_client", lambda *_: (Unusable(), "test-model"))


@pytest.fixture(autouse=True)
def _quiet_cabin(monkeypatch: pytest.MonkeyPatch) -> None:
    """No interruptions: their timers have nothing to do with resuming."""
    monkeypatch.setattr(game_module, "load_interruptions", lambda *_a, **_k: [])


@pytest.fixture(autouse=True)
def _own_data_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every session a test opens inside its own folder.

    ``session_dir`` resolves against the working directory, so moving there is
    what stops these from writing into the repo's real ``data/``.
    """
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def disk(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """Put one ROM on the machine's disk, or skip if the games were never fetched.

    ``Machine`` takes its games folder as a default argument, so the path is
    already bound and patching the module's ``GAMES_DIR`` would do nothing. The
    ROM is linked in under the same relative path instead. Only the tests that
    actually start a game need this; the ones about the conversation do not, and
    still run where there are no games at all.
    """
    original = Path(request.config.rootdir) / GAMES_DIR / ROM
    if not original.is_file():
        pytest.skip(f"no {original}; run the download to test resuming a game")

    games = tmp_path / GAMES_DIR
    games.mkdir(parents=True)
    (games / ROM).symlink_to(original)


async def sent_nowhere(_message: AssistantMessage) -> None:
    return None


async def play_a_little(session_id: UUID | None = None) -> UUID:
    """Open a session, get four moves and five points into Zork, and close it."""
    async with Game(ServerConfig(), sent_nowhere, session_id) as game:
        machine = game._machine
        await machine.new_game("zork1")
        for command in ("north", "north", "up", "take egg"):
            await machine.type_text(command)
        assert "Score 5/350" in machine.screen()
        return game.session_id


async def test_reopening_a_session_puts_the_game_back(disk: None) -> None:
    session_id = await play_a_little()

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        assert resumed._machine.game == "zork1"
        screen = resumed._machine.screen()
        assert "Score 5/350" in screen
        assert "Moves 4" in screen
        # The screen shows what was last printed, not a fresh look around: the
        # last thing typed was "take egg".
        assert "Taken." in screen


async def test_a_resumed_game_carries_on_rather_than_restarting(disk: None) -> None:
    session_id = await play_a_little()

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        await resumed._machine.type_text("down")
        assert "Moves 5" in resumed._machine.screen()


async def test_a_brand_new_session_comes_up_at_the_prompt(disk: None) -> None:
    async with Game(ServerConfig(), sent_nowhere) as fresh:
        assert fresh._machine.game is None
        assert "No game is running" in fresh._machine.screen()


async def test_a_session_id_with_nothing_behind_it_comes_up_at_the_prompt(disk: None) -> None:
    async with Game(ServerConfig(), sent_nowhere, uuid4()) as game:
        assert game._machine.game is None


async def test_reopening_a_session_brings_the_conversation_back() -> None:
    """The other half of a resume: what was said, not just what was played."""
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append({"role": "user", "content": "<stage_direction>say hello</stage_direction>"})
        first._append({"type": "reasoning", "encrypted_content": "shhh", "summary": []})
        first._append({"type": "function_call", "call_id": "c1", "name": "transmit"})
        first._append({"type": "function_call_output", "call_id": "c1", "output": "Transmitted."})
        said = list(first._messages)

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        assert resumed._messages == said


async def test_a_brand_new_session_starts_the_conversation_empty() -> None:
    async with Game(ServerConfig(), sent_nowhere) as fresh:
        assert fresh._messages == []
        assert not fresh._resumed


async def test_a_resumed_session_does_not_hail_the_operator_from_scratch() -> None:
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append({"role": "user", "content": "evening"})
        assert first.opening_direction() == OPENING_DIRECTION

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        # Picking a night back up, not opening a channel that was never open.
        assert resumed.opening_direction() == REOPENING_DIRECTION


async def test_the_conversation_keeps_growing_from_where_it_was_read() -> None:
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append({"role": "user", "content": "first night"})

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        resumed._append({"role": "user", "content": "second night"})

    async with Game(ServerConfig(), sent_nowhere, session_id) as again:
        assert [item["content"] for item in again._messages] == ["first night", "second night"]


def call(call_id: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": "type", "arguments": "{}"}


def result(call_id: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": "ok"}


def interrupted(call_id: str) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": INTERRUPTED_TOOL_OUTPUT,
    }


REASONING = {"type": "reasoning", "encrypted_content": "shhh", "summary": []}


def test_a_call_left_hanging_at_the_end_is_answered() -> None:
    items = [{"role": "user", "content": "hi"}, REASONING, call("a")]

    assert resolve_interrupted_calls(items) == [*items, interrupted("a")]


def test_half_a_batch_of_calls_is_finished_off() -> None:
    """Two calls asked for at once, killed after the first was answered."""
    items = [REASONING, call("a"), call("b"), result("a")]

    assert resolve_interrupted_calls(items) == [*items, interrupted("b")]


def test_a_conversation_that_needs_nothing_is_left_alone() -> None:
    items = [{"role": "user", "content": "hi"}, REASONING, call("a"), result("a")]

    assert resolve_interrupted_calls(items) == items


def test_an_empty_conversation_needs_nothing() -> None:
    assert resolve_interrupted_calls([]) == []


async def test_resuming_answers_the_call_and_mends_the_journal() -> None:
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append({"role": "user", "content": "hi"})
        first._append(REASONING)
        first._append(call("a"))  # and here the server stops
        journal = Path(first._journal or "")

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        assert resumed._messages[-1] == interrupted("a")
        # Mended on the disk too, not just in memory, so the next resume has
        # nothing left to mend.
        assert [json.loads(line) for line in journal.read_text().splitlines()] == resumed._messages

    async with Game(ServerConfig(), sent_nowhere, session_id) as again:
        assert again._messages == resumed._messages


async def test_the_repair_lands_before_anything_new_is_said() -> None:
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append(call("a"))

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        resumed._append({"role": "user", "content": "said after resuming"})

        kinds = [item.get("type") or item.get("role") for item in resumed._messages]
        assert kinds == ["function_call", "function_call_output", "user"]


def test_a_journal_that_is_not_there_reads_as_a_new_session(tmp_path: Path) -> None:
    assert load_journal(tmp_path / "messages.jsonl") == []


def test_a_line_that_is_not_an_object_is_dropped(tmp_path: Path) -> None:
    journal = tmp_path / "messages.jsonl"
    journal.write_text('{"role": "user", "content": "kept"}\n"a bare string"\n[]\n\n', "utf-8")

    assert load_journal(journal) == [{"role": "user", "content": "kept"}]


async def test_a_half_written_last_line_does_not_lose_the_night() -> None:
    async with Game(ServerConfig(), sent_nowhere) as first:
        session_id = first.session_id
        first._append({"role": "user", "content": "the good part"})
        journal = Path(first._journal or "")

    # A server killed mid-write leaves a line that will not parse.
    with journal.open("a", encoding="utf-8") as handle:
        handle.write('{"role": "user", "content": "cut off mid-')

    async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
        assert [item["content"] for item in resumed._messages] == ["the good part"]


async def test_the_autosave_lands_under_the_session(disk: None) -> None:
    session_id = await play_a_little()

    saves = Path("data/sessions") / str(session_id) / "server" / "saves"
    assert (saves / "autosave.bin").is_file()
    assert not list(saves.glob("[0-9][0-9][0-9][0-9].bin"))  # nothing asked for a slot


async def test_a_session_resumes_more_than_once(disk: None) -> None:
    session_id = await play_a_little()

    for expected in ("Moves 5", "Moves 6"):
        async with Game(ServerConfig(), sent_nowhere, session_id) as resumed:
            await resumed._machine.type_text("look")
            assert expected in resumed._machine.screen()
