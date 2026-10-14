"""Exercise interpreter failures with synthetic ROMs, without downloaded games."""

from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from fernz import FernzEnv, MemoryAccessError

from cabin_fever_x86_core.server._machine import NO_GAME, NOTHING_TO_SAVE, Machine
from cabin_fever_x86_core.server._saves import AUTOSAVE, SaveStore

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_later_states.zip"


def probe(tmp_path: Path, code: bytes | None = None) -> tuple[Machine, SaveStore]:
    games = tmp_path / "games"
    games.mkdir()
    with ZipFile(FIXTURES) as archive:
        story = bytearray(archive.read("v5_constant.z5"))
    if code is not None:
        story[0x401 : 0x401 + len(code)] = code
    (games / "probe.z5").write_bytes(story)
    store = SaveStore(tmp_path / "saves")
    return Machine(games_dir=games, saves=store), store


async def test_failed_execution_returns_to_prompt_and_preserves_autosave(tmp_path: Path) -> None:
    # Line input, then print_num pops an empty evaluation stack: real ExecutionError.
    machine, store = probe(tmp_path, bytes.fromhex("e4 0f 02 80 02 a0 1e e6 bf 00 ba"))
    await machine.new_game("probe")
    before = store.path(AUTOSAVE).read_bytes()
    screen, _ = await machine.type_text("look")
    assert "machine stopped" in screen
    assert machine.screen() == NO_GAME
    assert (await machine.type_text("\n"))[0] == NO_GAME
    assert await machine.save() == NOTHING_TO_SAVE
    assert "Bad command" in (await machine.type_text("look"))[0]
    assert store.path(AUTOSAVE).read_bytes() == before
    assert "Restored probe" in await machine.load(AUTOSAVE)
    machine.reboot()


async def test_rejected_input_does_not_discard_a_healthy_interpreter(tmp_path: Path) -> None:
    machine, store = probe(tmp_path)
    await machine.new_game("probe")
    state = machine._env.get_state()
    before = store.path(AUTOSAVE).read_bytes()
    assert "could not accept" in (await machine.type_text("bad\x01input"))[0]
    assert machine._env.get_state() == state
    assert store.path(AUTOSAVE).read_bytes() == before
    assert "R=" in (await machine.type_text("look"))[0]
    machine.reboot()


@pytest.mark.parametrize(
    "text,code", [("", 13), ("\n", 13), ("\r\n", 13), ("\t", 13), (" ", 32), (" \n", 32)]
)
async def test_character_prompt_accepts_enter_and_space(
    tmp_path: Path, text: str, code: int
) -> None:
    # read_char -> print_num result -> line input -> quit.
    program = bytes.fromhex("f6 7f 01 10 e6 bf 10 bb e4 0f 02 80 02 a0 1e ba")
    machine, _store = probe(tmp_path, program)
    await machine.new_game("probe")
    assert machine._env.awaiting_character
    screen, _ = await machine.type_text(text)
    assert str(code) in screen
    assert not machine._env.awaiting_character
    assert "The game has ended" in (await machine.type_text("look"))[0]
    machine.reboot()


@pytest.mark.parametrize("damage", ["hash", "signature"])
async def test_failed_resume_cannot_leave_a_fresh_game_or_overwrite_autosave(
    tmp_path: Path, damage: str
) -> None:
    machine, store = probe(tmp_path)
    await machine.new_game("probe")
    snapshot = store.read(AUTOSAVE, story=machine._env.story)
    if damage == "hash":
        snapshot = replace(snapshot, state=replace(snapshot.state, story_sha256="0" * 64))
    else:
        snapshot = replace(
            snapshot, story_signature=replace(snapshot.story_signature, serial="999999")
        )
    store.write(AUTOSAVE, snapshot)
    before = store.path(AUTOSAVE).read_bytes()
    machine.reboot()
    assert await machine.resume() is None
    assert machine.screen() == NO_GAME
    assert "Bad command" in (await machine.type_text("look"))[0]
    assert await machine.save() == NOTHING_TO_SAVE
    assert store.path(AUTOSAVE).read_bytes() == before


async def test_failed_cross_game_restore_keeps_the_running_game(tmp_path: Path) -> None:
    machine, store = probe(tmp_path)
    other = machine._games_dir / "other.z5"
    other.write_bytes((machine._games_dir / "probe.z5").read_bytes())
    await machine.new_game("probe")
    snapshot = store.read(AUTOSAVE, story=machine._env.story)
    store.write(
        "other_0001",
        replace(snapshot, game="other", state=replace(snapshot.state, story_sha256="0" * 64)),
    )
    state = machine._env.get_state()
    screen = machine.screen()
    before = store.path(AUTOSAVE).read_bytes()
    assert "different story image" in await machine.load("other_0001")
    assert machine.game == "probe"
    assert machine.screen() == screen
    assert machine._env.get_state() == state
    assert store.path(AUTOSAVE).read_bytes() == before
    assert "R=" in (await machine.type_text("look"))[0]
    machine.reboot()


async def test_restore_does_not_run_the_opening_program(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine, _store = probe(tmp_path)
    await machine.new_game("probe")
    screen = machine.screen()
    machine.reboot()

    def unexpected_reset(_env):
        pytest.fail("Loading a checkpoint must not execute reset()")

    monkeypatch.setattr(FernzEnv, "reset", unexpected_reset)
    assert "Restored probe" in (await machine.resume() or "")
    assert machine.screen() == screen
    machine.reboot()


async def test_unreadable_location_does_not_prevent_play_or_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def bad_location(_env):
        raise MemoryAccessError("Malformed optional object data")

    monkeypatch.setattr(FernzEnv, "get_player_location", bad_location)
    machine, store = probe(tmp_path)
    assert "GAME: probe" in await machine.new_game("probe")
    assert machine._location is None
    assert "R=" in (await machine.type_text("look"))[0]
    screen = machine.screen()
    machine.reboot()
    assert "Restored probe" in (await machine.resume() or "")
    assert machine.screen() == screen
    assert store.read(AUTOSAVE, story=machine._env.story).location is None
    machine.reboot()


async def test_input_and_failure_status_use_public_queries_without_checkpointing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Character input, then line input, then a fatal stack underflow.
    program = bytes.fromhex("f6 7f 01 10 e4 0f 02 80 02 a0 1e e6 bf 00 ba")
    machine, _store = probe(tmp_path, program)
    await machine.new_game("probe")
    machine._saves = None  # Status checks must not create checkpoints; autosaves do.

    def unexpected_checkpoint(_env):
        pytest.fail("Input mode and failure status must use Fernz's public properties")

    monkeypatch.setattr(FernzEnv, "get_state", unexpected_checkpoint)
    await machine.type_text("\n")
    assert machine._env.initialized and not machine._env.awaiting_character
    assert (await machine.type_text("\n"))[0] == machine.screen()
    assert "could not accept" in (await machine.type_text("bad\x01input"))[0]
    assert machine._env.initialized
    assert "machine stopped" in (await machine.type_text("look"))[0]
    assert machine.screen() == NO_GAME


async def test_restore_reuses_the_live_story_without_reopening_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fernz import Story

    machine, _store = probe(tmp_path)
    await machine.new_game("probe")
    original = machine._env
    screen = machine.screen()

    # A replaced ROM on disk must not change the identity of the live Story.
    path = machine._games_dir / "probe.z5"
    replacement = bytearray(path.read_bytes())
    replacement[0x12:0x18] = b"999999"
    path.write_bytes(replacement)
    signature = machine._story_signature

    def unexpected_read(*args):
        pytest.fail("Restore should reuse the loaded Story")

    monkeypatch.setattr(Story, "from_file", unexpected_read)
    assert "Restored probe" in await machine.load(AUTOSAVE)
    assert machine._env is not original
    assert machine._env.story is original.story
    assert machine._story_signature == signature
    assert machine.screen() == screen
    assert "R=" in (await machine.type_text("look"))[0]
    machine.reboot()
