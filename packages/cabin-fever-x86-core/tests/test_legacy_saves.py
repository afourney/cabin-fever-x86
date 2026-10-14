"""Import captured CFX86SAVE2 files without a runtime or test dependency on Jericho."""

from __future__ import annotations

import json
import re
from pathlib import Path
from zipfile import ZipFile

import pytest
from fernz import GameState

from cabin_fever_x86_core.server._machine import Machine
from cabin_fever_x86_core.server._saves import AUTOSAVE, LEGACY_MAGIC, MAGIC, SaveError, SaveStore

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_states.zip"
CASES = (
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


def captured(tmp_path: Path, case: str = "counting_stack") -> tuple[Path, SaveStore, dict]:
    games = tmp_path / "games"
    games.mkdir()
    store = SaveStore(tmp_path / "saves")
    store.dir.mkdir()
    with ZipFile(FIXTURES) as archive:
        (games / "probe.z3").write_bytes(archive.read(f"{case}.z3"))
        store.path("probe_0001").write_bytes(archive.read(f"{case}.bin"))
        expected = json.loads(archive.read("manifest.json"))[case]
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


async def test_unsupported_legacy_story_version_is_explicit(tmp_path: Path) -> None:
    games, store, _ = captured(tmp_path)
    path = games / "probe.z3"
    data = bytearray(path.read_bytes())
    data[0] = 4
    data[0x1A:0x1C] = (len(data) // 4).to_bytes(2, "big")
    path.write_bytes(data)
    machine = Machine(games_dir=games, saves=store)
    assert "V3 only" in await machine.load("probe_0001")
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


@pytest.mark.parametrize("case", CASES)
def test_copied_bridge_round_trip_preserves_input_and_frames(tmp_path: Path, case: str) -> None:
    from fernz import Story

    from cabin_fever_x86_core.server._state_bridge import StateBridge

    games, store, _ = captured(tmp_path, case)
    native = store.read("probe_0001").state
    bridge = StateBridge(Story.from_file(games / "probe.z3"), native, seed=42)
    state = bridge.from_jericho(native)
    assert bridge.from_jericho(bridge.to_jericho(state)) == state
