"""Durable memories shared across runs of one story file."""

import json
from pathlib import Path

import pytest

from cabin_fever_x86_core.server._game_memories import (
    GAME_FILE,
    MAP_FILE,
    GameMemoryError,
    GameMemoryStore,
    merge_maps,
)
from cabin_fever_x86_core.server._saves import KnownMap, StorySignature

SIGNATURE = StorySignature(release=88, serial="840726", checksum=0x1234)
OTHER_SIGNATURE = StorySignature(release=89, serial="850101", checksum=0x5678)
MAP: KnownMap = {
    (180, "West of House"): {(181, "North of House"): "north"},
}


def test_a_game_memory_uses_the_rom_name_and_round_trips(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")
    store.write_map("zork1", SIGNATURE, MAP)

    game_dir = store.game_dir("zork1")
    assert game_dir == tmp_path / "game-memories" / "zork1"
    assert store.read_map("zork1", SIGNATURE) == MAP

    game = json.loads((game_dir / GAME_FILE).read_text(encoding="utf-8"))
    map_record = json.loads((game_dir / MAP_FILE).read_text(encoding="utf-8"))
    assert game["game"] == "zork1"
    assert game["story_signature"] == SIGNATURE.as_record()
    assert "story_signature" not in map_record
    assert not (game_dir / "history.jsonl").exists()


def test_missing_game_memories_are_empty_and_create_no_folder(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")

    assert store.read_map("zork1", SIGNATURE) == {}
    assert not store.dir.exists()


def test_a_missing_map_in_existing_game_memories_is_empty(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")
    store.write_map("zork1", SIGNATURE, MAP)
    (store.game_dir("zork1") / MAP_FILE).unlink()

    assert store.read_map("zork1", SIGNATURE) == {}


def test_different_rom_names_have_independent_memories(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")
    other_map: KnownMap = {(1, "Start"): {(2, "End"): "east"}}
    store.write_map("zork1", SIGNATURE, MAP)
    store.write_map("advent", OTHER_SIGNATURE, other_map)

    assert store.read_map("zork1", SIGNATURE) == MAP
    assert store.read_map("advent", OTHER_SIGNATURE) == other_map


def test_incompatible_game_memories_are_rotated_together(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")
    store.write_map("zork1", SIGNATURE, MAP)

    assert store.read_map("zork1", OTHER_SIGNATURE) == {}
    assert not store.game_dir("zork1").exists()
    aside = store.dir / "zork1.0001"
    assert (aside / GAME_FILE).exists()
    assert (aside / MAP_FILE).exists()


def test_an_unreadable_map_is_not_silently_discarded(tmp_path: Path) -> None:
    store = GameMemoryStore(tmp_path / "game-memories")
    store.write_map("zork1", SIGNATURE, MAP)
    path = store.game_dir("zork1") / MAP_FILE
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(GameMemoryError, match="could not read"):
        store.read_map("zork1", SIGNATURE)
    assert path.read_text(encoding="utf-8") == "not json"


def test_merge_maps_is_additive() -> None:
    known: KnownMap = {
        (180, "West"): {(181, "North"): "north"},
    }
    run: KnownMap = {
        (181, "North"): {(182, "Behind"): "east"},
    }

    assert merge_maps(known, run)
    assert known == {
        (180, "West"): {(181, "North"): "north"},
        (181, "North"): {(182, "Behind"): "east"},
    }
    assert not merge_maps(known, run)
