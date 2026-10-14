"""Import captured CFX86SAVE2 files without a runtime or test dependency on Jericho."""

from __future__ import annotations

import json
import re
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest
from fernz import GameState

from cabin_fever_x86_core.server._machine import Machine
from cabin_fever_x86_core.server._saves import AUTOSAVE, LEGACY_MAGIC, MAGIC, SaveError, SaveStore

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_states.zip"
V3_CASES = (
    "host",
    "counting_stack",
    "affine",
    "global_switch",
    "local",
    "many_locals",
    "high_address",
    "parent_frame",
    "root_frame",
)
LATER_FIXTURES = FIXTURES.with_name("legacy_later_states.zip")
with ZipFile(LATER_FIXTURES) as archive:
    LATER_CASES = tuple(json.loads(archive.read("manifest.json")))
CASES = (*V3_CASES, *LATER_CASES)


def captured(tmp_path: Path, case: str = "counting_stack") -> tuple[Path, SaveStore, dict]:
    games = tmp_path / "games"
    games.mkdir()
    store = SaveStore(tmp_path / "saves")
    store.dir.mkdir()
    with ZipFile(LATER_FIXTURES if case in LATER_CASES else FIXTURES) as archive:
        expected = json.loads(archive.read("manifest.json"))[case]
        version = expected.get("version", 3)
        (games / f"probe.z{version}").write_bytes(archive.read(f"{case}.z{version}"))
        store.path("probe_0001").write_bytes(archive.read(f"{case}.bin"))
    return games, store, expected


def rewrite_header(path: Path, **changes: object) -> None:
    magic, header, rest = path.read_bytes().split(b"\n", 2)
    metadata = json.loads(header)
    metadata.update(changes)
    path.write_bytes(magic + b"\n" + json.dumps(metadata).encode() + b"\n" + rest)


@pytest.mark.parametrize("case", CASES)
async def test_captured_legacy_saves_continue_like_jericho(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    games, store, expected = captured(tmp_path, case)
    monkeypatch.setattr("cabin_fever_x86_core.server._machine._fresh_seed", lambda: 42)
    path = store.path("probe_0001")
    original = path.read_bytes()
    legacy = store.read("probe_0001")
    assert isinstance(legacy.state, tuple)
    machine = Machine(games_dir=games, saves=store)
    try:
        assert "Restored probe" in await machine.load("probe_0001")
        assert machine._observation == legacy.observation
        assert path.read_bytes() == original
        assert original.startswith(LEGACY_MAGIC + b"\n")
        assert store.path(AUTOSAVE).read_bytes().startswith(MAGIC + b"\n")
        imported = store.read(AUTOSAVE)
        assert isinstance(imported.state, GameState)
        if expected["rng"][1]:
            assert imported.state.rng.period == expected["rng"][1]
            assert imported.state.rng.position == expected["rng"][2]
        else:
            assert imported.state.rng.state == 42
        for step in expected["continuation"]:
            screen, _ = await machine.type_text("look")
            assert "locks up" not in screen
            # These synthetic stories print only tagged numeric results. Jericho
            # also renders its status line; Fernz deliberately omits that line.
            assert re.findall(r"^R=(-?\d+)$", machine._observation, re.MULTILINE) == re.findall(
                r"^R=(-?\d+)$", step["text"], re.MULTILINE
            )
            assert machine._done == step["done"]
        assert machine._done
    finally:
        machine.reboot()


async def test_legacy_autosave_is_upgraded_and_resumes_again(tmp_path: Path) -> None:
    games, store, _ = captured(tmp_path)
    store.path("probe_0001").rename(store.path(AUTOSAVE))
    first = Machine(games_dir=games, saves=store)
    second = Machine(games_dir=games, saves=store)
    try:
        assert "Restored probe" in (await first.resume() or "")
        assert store.path(AUTOSAVE).read_bytes().startswith(MAGIC + b"\n")
        screen = first.screen()
        first.reboot()
        assert "Restored probe" in (await second.resume() or "")
        assert second.screen() == screen
        assert "locks up" not in (await second.type_text("look"))[0]
    finally:
        first.reboot()
        second.reboot()


@pytest.mark.parametrize(
    "field,value",
    [
        ("opcode", 0xBA),
        ("pc", 0),
        ("fp", 1025),
        ("frame_count", 0),
        ("rng", [42, 5, 5]),
        ("ram_size", -1),
        ("stack_size", 4096),
        ("pc", "100"),
    ],
)
async def test_bad_legacy_state_preserves_the_running_game(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    games, store, _ = captured(tmp_path)
    path = store.path("probe_0001")
    rewrite_header(path, **{field: value})
    before_file = path.read_bytes()
    machine = Machine(games_dir=games, saves=store)
    try:
        await machine.new_game("probe")
        screen = machine.screen()
        state = machine._env.get_state()
        autosave = store.path(AUTOSAVE).read_bytes()
        result = await machine.load("probe_0001")
        assert "cannot be converted" in result or "invalid legacy machine state" in result
        assert machine.screen() == screen
        assert machine._env.get_state() == state
        assert store.path(AUTOSAVE).read_bytes() == autosave
        assert path.read_bytes() == before_file
    finally:
        machine.reboot()


async def test_legacy_story_version_mismatch_is_rejected(tmp_path: Path) -> None:
    games, store, _ = captured(tmp_path)
    path = games / "probe.z3"
    data = bytearray(path.read_bytes())
    data[0] = 4
    data[0x1A:0x1C] = (len(data) // 4).to_bytes(2, "big")
    path.write_bytes(data)
    machine = Machine(games_dir=games, saves=store)
    assert "cannot be converted" in await machine.load("probe_0001")
    assert machine.game is None
    assert not store.path(AUTOSAVE).exists()


async def test_legacy_import_rejects_mismatched_story_header(tmp_path: Path) -> None:
    games, store, _ = captured(tmp_path)
    path = games / "probe.z3"
    data = bytearray(path.read_bytes())
    data[0x12:0x18] = b"999999"
    path.write_bytes(data)
    machine = Machine(games_dir=games, saves=store)
    assert "cannot be converted" in await machine.load("probe_0001")
    assert machine.game is None
    assert not store.path(AUTOSAVE).exists()


def test_legacy_metadata_and_personal_map_are_retained(tmp_path: Path) -> None:
    _games, store, _ = captured(tmp_path)
    path = store.path("probe_0001")
    magic, header, _, tail = path.read_bytes().split(b"\n", 3)
    route = {
        "type": "personal_map",
        "version": 1,
        "edges": [
            {"from": [1, "Old Room"], "to": [2, "Another Room"], "command": "north"},
        ],
    }
    path.write_bytes(magic + b"\n" + header + b"\n" + json.dumps(route).encode() + b"\n" + tail)
    saved = store.read("probe_0001")
    assert saved.comment == "Captured from Jericho 3.3.1"
    assert saved.personal_map == {(1, "Old Room"): {(2, "Another Room"): "north"}}


def test_truncated_legacy_save_is_refused(tmp_path: Path) -> None:
    _games, store, _ = captured(tmp_path)
    path = store.path("probe_0001")
    path.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(SaveError, match="truncated"):
        store.read("probe_0001")


@pytest.mark.parametrize("case", [case for case in CASES if not case.endswith("_untimed")])
def test_copied_bridge_round_trip_preserves_input_and_frames(tmp_path: Path, case: str) -> None:
    from fernz import Story

    from cabin_fever_x86_core.server._state_bridge import StateBridge

    games, store, _ = captured(tmp_path, case)
    native = store.read("probe_0001").state
    bridge = StateBridge(Story.from_file(next(games.glob("probe.z*"))), native, seed=42)
    state = bridge.from_jericho(native)
    assert bridge.from_jericho(bridge.to_jericho(state)) == state


@pytest.mark.parametrize("version", (4, 5, 8))
async def test_legacy_active_timer_is_rejected_before_boot(tmp_path: Path, version: int) -> None:
    games, store, _ = captured(tmp_path, f"v{version}_untimed")
    native = store.read("probe_0001").state
    path = games / f"probe.z{version}"
    rom = bytearray(path.read_bytes())
    # Four-operand read: two large buffer addresses followed by two small
    # timeout operands. Nonzero time AND routine would require timed input.
    rom[native[2] + 5 : native[2] + 7] = b"\x01\x01"
    path.write_bytes(rom)
    machine = Machine(games_dir=games, saves=store)
    assert "Timed input is not supported" in await machine.load("probe_0001")
    assert machine.game is None
    assert not store.path(AUTOSAVE).exists()


@pytest.mark.parametrize("case", LATER_CASES)
def test_later_frames_and_read_result_are_preserved(tmp_path: Path, case: str) -> None:
    from fernz import FernzEnv, Story
    from fernz._state import StoredInputRequest

    from cabin_fever_x86_core.server._state_bridge import StateBridge

    games, store, expected = captured(tmp_path, case)
    native = store.read("probe_0001").state
    story = Story.from_file(next(games.glob("probe.z*")))
    state = StateBridge(story, native, seed=42).from_jericho(native)
    with closing(FernzEnv(next(games.glob("probe.z*")), seed=42)) as env:
        env.reset()
        original = env.get_state()
    assert state.frames == original.frames
    assert state.ram == original.ram
    assert state.pc == original.pc
    assert state.input_request == original.input_request
    assert (state.rng.period, state.rng.position) == (original.rng.period, original.rng.position)
    frame = state.frames[-1]
    assert len(frame.locals) == 15
    assert frame.argument_count == (7 if case.endswith("wide") else 3)
    assert (frame.store is None) == ("discard" in case)
    if expected["version"] >= 5:
        assert isinstance(state.input_request, StoredInputRequest)
        assert state.input_request.store == 30
    if case == "v8_high":
        assert frame.return_address > 0x40000


@pytest.mark.parametrize("version", (4, 5, 8))
@pytest.mark.parametrize("flags", (0x2000, 0x0008))
def test_unmeasured_frame_flags_are_rejected(tmp_path: Path, version: int, flags: int) -> None:
    from fernz import Story

    from cabin_fever_x86_core.server._state_bridge import StateBridge

    games, store, _ = captured(tmp_path, f"v{version}_constant")
    native = store.read("probe_0001").state
    bridge = StateBridge(Story.from_file(next(games.glob("probe.z*"))), native, seed=42)
    stack = native[1].copy()
    stack[2 * native[4] : 2 * native[4] + 2] = list(flags.to_bytes(2, "little"))
    malformed = (native[0], stack, *native[2:])
    with pytest.raises(ValueError, match="frame metadata"):
        bridge.from_jericho(malformed)


async def test_invalid_legacy_instruction_is_reported_without_losing_live_game(
    tmp_path: Path,
) -> None:
    games, store, _ = captured(tmp_path)
    machine = Machine(games_dir=games, saves=store)
    await machine.new_game("probe")
    screen = machine.screen()
    state = machine._env.get_state()
    autosave = store.path(AUTOSAVE).read_bytes()
    source = store.path("probe_0001").read_bytes()
    native = store.read("probe_0001").state
    path = games / "probe.z3"
    rom = bytearray(path.read_bytes())
    rom[native[2]] = 0xFF  # Zero operands on sread raises Fernz ExecutionError.
    path.write_bytes(rom)
    assert "cannot be converted" in await machine.load("probe_0001")
    assert machine.screen() == screen
    assert machine._env.get_state() == state
    assert store.path(AUTOSAVE).read_bytes() == autosave
    assert store.path("probe_0001").read_bytes() == source
    machine.reboot()


@pytest.mark.parametrize(
    "platform,byteorder", [("darwin", "little"), ("win32", "little"), ("linux", "big")]
)
def test_legacy_layout_is_independent_of_reader_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, byteorder: str
) -> None:
    import sys

    from fernz import Story

    from cabin_fever_x86_core.server._state_bridge import StateBridge

    games, store, _ = captured(tmp_path, "v8_discard_wide")
    native = store.read("probe_0001").state
    story = Story.from_file(games / "probe.z8")
    expected = StateBridge(story, native, seed=42).from_jericho(native)
    # The fixed little-endian file encoding must not depend on host markers.
    # This tests the codec; it does not claim end-to-end Windows app validation.
    with monkeypatch.context() as context:
        context.setattr(sys, "platform", platform)
        context.setattr(sys, "byteorder", byteorder)
        actual = StateBridge(story, native, seed=42).from_jericho(native)
    assert actual == expected
