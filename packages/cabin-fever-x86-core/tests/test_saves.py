"""Saved games: the store's file format, and the machine driving it.

The store is tested on its own with a made-up state tuple, so the format and
the naming are pinned down without a ROM. The tests that need a real
interpreter skip themselves when the games have not been downloaded, which is
how CI runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from cabin_fever_x86_core.server._machine import GAMES_DIR, NO_GAME, USE_THE_TOOLS, Machine
from cabin_fever_x86_core.server._saves import (
    AUTOSAVE,
    LAST_SLOT,
    MAGIC,
    SUFFIX,
    SaveError,
    SaveStore,
    Snapshot,
    parse_name,
)
from cabin_fever_x86_core.server._tools import (
    ListGamesTool,
    ListSavedGamesTool,
    LoadGameTool,
    NewGameTool,
    RebootTool,
    SaveGameTool,
)

# Shaped like what Jericho hands out — (ram, stack, pc, sp, fp, frame_count,
# opcode, rng, narrative) — without needing Jericho to hand it out.
FAKE_STATE = (b"\x01\x02\x03\x04", b"\x05\x06", 22797, 989, 1000, 2, 228, (-146906654, 0, 0), b"hi")


def snapshot(game: str = "zork1", moves: int = 3) -> Snapshot:
    return Snapshot(
        game=game,
        state=FAKE_STATE,
        observation="West of House\nThere is a small mailbox here.",
        score=10,
        moves=moves,
        done=False,
    )


def test_the_machines_messages_name_the_tools_that_exist() -> None:
    """The messages point at tools by name, so a rename must not leave prose behind."""
    for tool in (SaveGameTool, ListSavedGamesTool, LoadGameTool, NewGameTool, RebootTool):
        assert tool.name in USE_THE_TOOLS, f"{tool.__name__} is not offered"
    for tool in (ListGamesTool, NewGameTool, ListSavedGamesTool, LoadGameTool):
        assert tool.name in NO_GAME, f"{tool.__name__} is not offered at the prompt"


def test_a_name_is_forgiven_its_spelling() -> None:
    assert parse_name("zork1_0001") == ("zork1", 1)
    assert parse_name("ZORK1_0001.BIN") == ("zork1", 1)
    assert parse_name("  zork1_42 ") == ("zork1", 42)
    assert parse_name(f"advent_{LAST_SLOT}") == ("advent", 9999)
    assert parse_name("  AutoSave.BIN ") is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "0000",
        "10000",
        "-1",
        "wibble",
        "1.5",
        "0001",  # each game counts from one, so a number alone names nothing
        "1",
        "zork1_0000",
        "zork1_10000",
        "../../etc/passwd",
        "autosave/../0001",
        "../etc_0001",
        "/abs_0001",
        ".._0001",
    ],
)
def test_anything_else_is_refused(name: str) -> None:
    with pytest.raises(SaveError):
        parse_name(name)


def test_a_save_round_trips(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    assert store.write("zork1_0001", snapshot()) == "zork1_0001"

    read = store.read("ZORK1_1.bin")  # and the sloppy spelling still finds it
    assert read.game == "zork1"
    assert read.observation.startswith("West of House")
    assert (read.score, read.moves, read.done) == (10, 3, False)

    ram, stack, *scalars, rng, narrative = read.state
    assert bytes(ram) == FAKE_STATE[0]
    assert bytes(stack) == FAKE_STATE[1]
    assert scalars == list(FAKE_STATE[2:7])
    assert rng == FAKE_STATE[7]
    assert narrative == FAKE_STATE[8]
    # set_state hands the buffers to ctypes, which will not take a read-only view.
    assert ram.flags.writeable and stack.flags.writeable


def test_a_state_of_the_wrong_shape_is_refused_on_the_way_in(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    bad = Snapshot(game="zork1", state=(b"", b"", 1), observation="", score=0, moves=0, done=False)
    with pytest.raises(SaveError, match="fields of machine state"):
        store.write("zork1_0001", bad)


def test_a_truncated_save_is_refused(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write("zork1_0001", snapshot())

    path = store.dir / f"zork1_0001{SUFFIX}"
    whole = path.read_bytes()
    path.write_bytes(whole[:-3])  # the state stops partway through

    with pytest.raises(SaveError, match="truncated"):
        store.read("zork1_0001")


def test_a_header_missing_the_state_is_refused(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.dir.mkdir(parents=True)
    header = json.dumps({"game": "zork1", "moves": 1}).encode("utf-8")
    (store.dir / f"zork1_0001{SUFFIX}").write_bytes(MAGIC + b"\n" + header + b"\n")

    # The listing still manages, since a header is all it ever wanted.
    assert [save.name for save in store.list()] == ["zork1_0001"]
    with pytest.raises(SaveError, match="missing part of its machine state"):
        store.read("zork1_0001")


def test_the_folder_is_made_on_the_first_write_and_not_before(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    assert not store.dir.exists()
    assert store.list() == []

    store.write(AUTOSAVE, snapshot())
    assert (store.dir / f"{AUTOSAVE}{SUFFIX}").is_file()


def test_a_save_is_readable_with_head_minus_two(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write("zork1_0007", snapshot())

    path = store.dir / f"zork1_0007{SUFFIX}"
    with path.open("rb") as handle:
        assert handle.readline().rstrip(b"\n") == MAGIC
        header = json.loads(handle.readline())

    assert header["game"] == "zork1"
    assert header["moves"] == 3
    assert header["pc"] == FAKE_STATE[2]
    assert header["rng"] == list(FAKE_STATE[7])
    assert header["ram_size"] == len(FAKE_STATE[0])
    assert "saved_at" in header

    # The state is the header's byte runs and nothing else.
    body = len(MAGIC) + 1 + len(json.dumps(header)) + 1
    assert path.stat().st_size == body + sum(
        header[name] for name in ("ram_size", "stack_size", "narrative_size")
    )


def test_a_finished_save_leaves_no_working_file(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(AUTOSAVE, snapshot())
    store.write(AUTOSAVE, snapshot(moves=4))  # overwritten in place

    assert sorted(p.name for p in store.dir.iterdir()) == [f"{AUTOSAVE}{SUFFIX}"]
    assert store.read(AUTOSAVE).moves == 4


def test_slots_count_up_from_the_highest_on_disk(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    assert store.next_name("zork1") == "zork1_0001"

    store.write(store.next_name("zork1"), snapshot())
    store.write(store.next_name("zork1"), snapshot())
    assert sorted(p.stem for p in store.dir.glob(f"*{SUFFIX}")) == ["zork1_0001", "zork1_0002"]

    # A gap left by a deleted save is not filled again.
    (store.dir / f"zork1_0001{SUFFIX}").unlink()
    assert store.next_name("zork1") == "zork1_0003"


def test_the_autosave_does_not_take_a_slot(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(AUTOSAVE, snapshot())
    assert store.next_name("zork1") == "zork1_0001"


def test_a_full_disk_is_reported(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(f"zork1_{LAST_SLOT}", snapshot())
    with pytest.raises(SaveError, match="full"):
        store.next_name("zork1")
    assert store.next_name("advent") == "advent_0001"  # another game is unaffected


def test_a_listing_survives_junk_in_the_folder(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(AUTOSAVE, snapshot())
    store.write("advent_0001", snapshot(game="advent"))
    (store.dir / f"zork1_0002{SUFFIX}").write_bytes(b"not a save\n")
    (store.dir / f"zork1_0003{SUFFIX}").write_bytes(MAGIC + b"\n{bad json\nxx")
    (store.dir / "notes.txt").write_text("shopping list", encoding="utf-8")

    listing = store.list()
    assert [save.name for save in listing] == ["advent_0001"]  # no autosave in a listing
    assert listing[0].game == "advent"
    assert "score 10" in listing[0].describe()


def test_a_save_is_named_for_its_game(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(store.next_name("zork1"), snapshot())
    store.write(store.next_name("advent"), snapshot(game="advent"))
    store.write(store.next_name("zork1"), snapshot())

    # Each game counts from one, so both have an 0001 and they do not collide.
    assert sorted(p.name for p in store.dir.iterdir()) == [
        f"advent_0001{SUFFIX}",
        f"zork1_0001{SUFFIX}",
        f"zork1_0002{SUFFIX}",
    ]
    assert [save.name for save in store.list()] == ["advent_0001", "zork1_0001", "zork1_0002"]


def test_two_games_each_have_their_own_0001(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(store.next_name("zork1"), snapshot())
    store.write(store.next_name("advent"), snapshot(game="advent"))

    assert store.read("zork1_0001").game == "zork1"
    assert store.read("advent_0001").game == "advent"
    assert store.read("ADVENT_1.bin").game == "advent"  # however it is spelled


def test_a_listing_says_the_game_once(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write("zork1_0001", snapshot())

    described = store.list()[0].describe()
    assert described.startswith("zork1_0001")
    assert described.count("zork1") == 1


def test_the_autosave_is_reachable_by_name_but_never_listed(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.write(AUTOSAVE, snapshot())

    assert store.list() == []
    assert store.read(AUTOSAVE).game == "zork1"  # the resume point, still there
    assert store.info(AUTOSAVE).moves == 3


def test_a_file_that_is_not_ours_is_refused_before_it_is_read(tmp_path: Path) -> None:
    store = SaveStore(tmp_path / "saves")
    store.dir.mkdir(parents=True)
    (store.dir / f"zork1_0001{SUFFIX}").write_bytes(b"SOMETHING-ELSE-ENTIRELY\n")

    with pytest.raises(SaveError, match="not a save from this machine"):
        store.read("zork1_0001")


def test_a_save_that_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(SaveError, match="no save called"):
        SaveStore(tmp_path / "saves").read("zork1_0500")


# --- with a real interpreter behind it ------------------------------------


def rom(name: str) -> Path:
    """A game to test against, or a skip if the disk was never filled."""
    path = GAMES_DIR / f"{name}.z5"
    if not path.is_file():
        pytest.skip(f"no {path}; run the download to test against a real game")
    return path


@pytest.fixture
def store(tmp_path: Path) -> SaveStore:
    return SaveStore(tmp_path / "saves")


@pytest.fixture
def machine(store: SaveStore) -> Iterator[Machine]:
    machine = Machine(saves=store)
    yield machine
    machine.reboot()  # frees the interpreter, whatever the test did


async def test_the_autosave_keeps_up_with_the_screen(machine: Machine, store: SaveStore) -> None:
    rom("zork1")
    await machine.type_text("zork1")

    assert store.read(AUTOSAVE).moves == 0  # a restore point at move zero

    await machine.type_text("north")
    await machine.type_text("north")
    assert store.read(AUTOSAVE).moves == 2


async def test_a_save_comes_back_where_it_was_left(machine: Machine) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    await machine.type_text("north")
    await machine.type_text("north")
    was = machine.screen()

    assert await machine.save() == "Saved as zork1_0001."

    await machine.type_text("south")
    await machine.type_text("south")
    assert machine.screen() != was

    assert "Restored zork1 from zork1_0001" in await machine.load("zork1_0001")
    assert machine.screen() == was


async def test_a_save_outlives_a_reboot(machine: Machine) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    await machine.type_text("north")
    was = machine.screen()
    await machine.save()

    machine.reboot()
    assert machine.game is None

    await machine.load("zork1_0001")
    assert machine.game == "zork1"
    assert machine.screen() == was


async def test_loading_another_game_changes_the_disk_in_the_drive(machine: Machine) -> None:
    rom("zork1")
    rom("advent")
    await machine.type_text("advent")
    await machine.save()

    machine.reboot()
    await machine.type_text("zork1")
    assert machine.game == "zork1"

    assert "Restored advent" in await machine.load("advent_0001")
    assert machine.game == "advent"


@pytest.mark.parametrize("line", ["save", "SAVE", " save ", "save.", "save game", "save my game"])
async def test_the_games_own_save_never_reaches_it(machine: Machine, line: str) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    was = machine.screen()

    answer = await machine.type_text(line)
    assert answer == USE_THE_TOOLS
    assert machine.screen() == was  # no move was spent on it


@pytest.mark.parametrize("line", ["restore", "RESTORE", "restore.", "restore my game"])
async def test_the_games_own_restore_never_reaches_it(machine: Machine, line: str) -> None:
    rom("zork1")
    await machine.type_text("zork1")

    assert await machine.type_text(line) == USE_THE_TOOLS


@pytest.mark.parametrize("line", ["quit", "q", "QUIT", "quit.", "restart", "die", "quit game"])
async def test_nothing_that_stops_to_ask_reaches_the_game(machine: Machine, line: str) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    was = machine.screen()

    answer = await machine.type_text(line)
    assert answer == USE_THE_TOOLS
    assert machine.screen() == was


async def test_the_command_after_a_quit_is_not_swallowed(machine: Machine) -> None:
    """The point of holding `quit` back: the next line still reaches the game.

    Typed through, `quit` leaves a yes/no question hanging and the move after it
    is eaten answering it — the companion is told "Ok." and reports a move that
    never happened.
    """
    rom("zork1")
    await machine.type_text("zork1")
    await machine.type_text("north")
    assert "Moves 1" in machine.screen()

    await machine.type_text("quit")
    assert "Moves 1" in machine.screen()  # no time passed on it

    await machine.type_text("north")
    assert "Moves 2" in machine.screen()  # the move actually happened


async def test_a_restart_cannot_wipe_the_night_by_accident(machine: Machine) -> None:
    """`restart` followed by a "yes" really does reset the game, if it gets through."""
    rom("zork1")
    await machine.type_text("zork1")
    for command in ("north", "north", "up", "take egg"):
        await machine.type_text(command)
    assert "Score 5/350" in machine.screen()

    await machine.type_text("restart")
    await machine.type_text("yes")  # would have confirmed it

    assert "Score 5/350" in machine.screen()
    assert "Moves 4" in machine.screen()


@pytest.mark.parametrize("line", ["quiver", "question", "quaff", "z", "g"])
async def test_a_verb_that_merely_starts_the_same_way_still_goes_through(
    machine: Machine, line: str
) -> None:
    rom("zork1")
    await machine.type_text("zork1")

    assert await machine.type_text(line) != USE_THE_TOOLS


@pytest.mark.parametrize("line", ["load", "load game", "load the crossbow"])
async def test_load_is_left_to_the_game_to_refuse(machine: Machine, line: str) -> None:
    """`load` is not a save verb in any of these games, so there is nothing to hold back.

    The parse error is the truth about the machine, and better than an answer
    invented here for a command the game does not have.
    """
    rom("zork1")
    await machine.type_text("zork1")

    answer = await machine.type_text(line)
    assert answer != USE_THE_TOOLS
    assert "load" in answer.casefold()  # the game's own complaint about the word


@pytest.mark.parametrize("line", ["save here", "restore here"])
async def test_a_games_own_noise_words_cannot_slip_one_past(machine: Machine, line: str) -> None:
    """Zork really does save on 'save here': "here" is one of its noise words.

    Which is why the verb decides and not the whole line — a list of phrasings
    would have let this one through.
    """
    rom("zork1")
    await machine.type_text("zork1")

    assert await machine.type_text(line) == USE_THE_TOOLS


async def test_no_quetzal_file_is_left_anywhere(machine: Machine, tmp_path: Path) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    before = sorted(Path.cwd().glob("*.qzl"))

    await machine.type_text("save")
    await machine.type_text("save game")

    assert sorted(Path.cwd().glob("*.qzl")) == before
    assert list(tmp_path.glob("**/*.qzl")) == []


@pytest.mark.parametrize("line", ["load the crossbow", "unsave", "saves", "savour the moment"])
async def test_a_real_command_is_not_mistaken_for_a_save(machine: Machine, line: str) -> None:
    rom("zork1")
    await machine.type_text("zork1")

    answer = await machine.type_text(line)
    assert answer != USE_THE_TOOLS  # the game answered, not us


async def test_save_with_an_object_is_held_back_too(machine: Machine) -> None:
    """SAVE is a meta verb everywhere, so there is no `save the princess` to lose.

    Measured across the six games in the suite, anything after the verb is a
    parser error rather than an action, so taking the whole line costs nothing.
    """
    rom("zork1")
    await machine.type_text("zork1")

    assert await machine.type_text("save the princess") == USE_THE_TOOLS


async def test_a_save_command_at_the_dos_prompt_is_caught_too(machine: Machine) -> None:
    assert machine.game is None
    assert "save_game" in await machine.type_text("save")
    assert machine.game is None  # and it was not mistaken for a game to boot


async def test_a_new_game_starts_over_whatever_was_running(machine: Machine) -> None:
    rom("zork1")
    rom("advent")
    await machine.type_text("zork1")
    await machine.type_text("north")
    assert "Moves 1" in machine.screen()

    # No reboot needed, and no need to be at the DOS prompt.
    screen = await machine.new_game("advent")
    assert machine.game == "advent"
    assert "Moves 0" in screen


async def test_a_new_game_starts_the_same_one_from_the_top(machine: Machine) -> None:
    rom("zork1")
    await machine.type_text("zork1")
    for command in ("north", "north", "up", "take egg"):
        await machine.type_text(command)
    assert "Score 5/350" in machine.screen()

    await machine.new_game("zork1")
    assert "Score 0/350" in machine.screen()
    assert "Moves 0" in machine.screen()


@pytest.mark.parametrize("name", ["zork1", "ZORK1", "zork1.z5", " zork1 "])
async def test_a_new_game_is_as_forgiving_as_the_prompt(machine: Machine, name: str) -> None:
    rom("zork1")
    assert machine.game is None
    await machine.new_game(name)
    assert machine.game == "zork1"


@pytest.mark.parametrize("name", ["zork", "wibble"])
async def test_a_name_that_names_no_one_game_is_refused_with_the_disk(
    machine: Machine, name: str
) -> None:
    rom("zork1")
    answer = await machine.new_game(name)
    assert "Bad command or file name" in answer
    assert "zork1" in answer  # the listing, so it can pick a real one
    assert machine.game is None


async def test_a_new_game_leaves_a_restore_point_at_move_zero(
    machine: Machine, store: SaveStore
) -> None:
    rom("zork1")
    await machine.new_game("zork1")

    assert store.read(AUTOSAVE).game == "zork1"
    assert store.read(AUTOSAVE).moves == 0


async def test_a_second_machine_resumes_where_the_first_left_off(store: SaveStore) -> None:
    """What a reopened session does: a fresh machine, the same saves folder."""
    rom("zork1")
    first = Machine(saves=store)
    await first.new_game("zork1")
    for command in ("north", "north", "up", "take egg"):
        await first.type_text(command)
    was = first.screen()
    first.reboot()

    second = Machine(saves=store)
    assert second.game is None
    assert await second.resume() is not None
    assert second.game == "zork1"
    assert second.screen() == was
    second.reboot()


async def test_resuming_a_session_that_never_played_anything(machine: Machine) -> None:
    assert await machine.resume() is None
    assert machine.game is None  # still at the prompt, and that is not an error


async def test_an_autosave_that_will_not_load_leaves_the_prompt(store: SaveStore) -> None:
    rom("zork1")
    store.dir.mkdir(parents=True)
    (store.dir / f"{AUTOSAVE}{SUFFIX}").write_bytes(MAGIC + b"\n{bad json\nxx")

    machine = Machine(saves=store)
    assert await machine.resume() is None
    assert machine.game is None


async def test_the_resumed_machine_can_still_be_played(store: SaveStore) -> None:
    rom("zork1")
    first = Machine(saves=store)
    await first.new_game("zork1")
    await first.type_text("north")
    first.reboot()

    second = Machine(saves=store)
    await second.resume()
    await second.type_text("north")
    assert "Moves 2" in second.screen()  # carried on from move 1, not from zero
    second.reboot()


async def test_nothing_to_save_is_said_rather_than_raised(machine: Machine) -> None:
    assert "nothing to save" in await machine.save()
    assert "will not load" in await machine.load("zork1_0001")
    assert machine.list_saves() == []
