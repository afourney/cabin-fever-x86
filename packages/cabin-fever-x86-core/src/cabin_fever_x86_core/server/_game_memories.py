"""Durable things Sam and the operator remember about one story file."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from cabin_fever_x86_core.server._saves import (
    KnownMap,
    StorySignature,
    decode_map_edges,
    encode_map_edges,
)

logger = logging.getLogger(__name__)

GAME_MEMORIES_DIR = "game-memories"
GAME_FILE = "game.json"
MAP_FILE = "map.json"
RELOAD_REASONS_FILE = "reload_reasons.jsonl"
GAME_MEMORY_VERSION = 1
MAP_VERSION = 1
_SAFE_GAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class GameMemoryError(Exception):
    """A game's durable memory could not be read or written safely."""


class GameMemoryStore:
    """One ROM-named directory of memories, guarded by its story signature."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.dir = Path(directory)

    def game_dir(self, game: str) -> Path:
        if not _SAFE_GAME.fullmatch(game):
            raise GameMemoryError(f"{game!r} is not a safe game name")
        return self.dir / game

    def read_map(self, game: str, signature: StorySignature) -> KnownMap:
        """Read a matching game's map, rotating incompatible memories aside."""
        game_dir = self.game_dir(game)
        if not game_dir.exists():
            return {}
        if not game_dir.is_dir():
            raise GameMemoryError(f"{game_dir.name} is not a game-memory directory")
        try:
            matches = self._matches(game_dir, game, signature)
        except GameMemoryError as exc:
            aside = self._rotate(game_dir)
            logger.warning(
                "Game memories in %s are unreadable (%s); moved them to %s",
                game_dir.name,
                exc,
                aside.name,
            )
            return {}
        if not matches:
            aside = self._rotate(game_dir)
            logger.warning(
                "Game memories in %s belong to another build; moved them to %s",
                game_dir.name,
                aside.name,
            )
            return {}

        path = game_dir / MAP_FILE
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GameMemoryError(f"could not read {game}/{MAP_FILE}: {exc}") from exc
        try:
            if not isinstance(value, dict):
                raise ValueError("top level is not an object")
            if value.get("version") != MAP_VERSION:
                raise ValueError(f"unsupported version {value.get('version')!r}")
            return decode_map_edges(value.get("edges"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GameMemoryError(f"{game}/{MAP_FILE} is unreadable: {exc}") from exc

    def write_map(self, game: str, signature: StorySignature, known_map: KnownMap) -> None:
        """Atomically replace one ROM's map, creating its manifest if needed."""
        game_dir = self._prepare(game, signature)

        self._write_json(
            game_dir / MAP_FILE,
            {
                "version": MAP_VERSION,
                "edges": encode_map_edges(known_map),
            },
        )

    def append_reload_reason(
        self,
        game: str,
        signature: StorySignature,
        location: str,
        reason: str,
    ) -> None:
        """Remember why play was rewound from a room."""
        game_dir = self._prepare(game, signature)
        path = game_dir / RELOAD_REASONS_FILE
        record = json.dumps(
            {"location": location, "reason": reason},
            ensure_ascii=False,
        )
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(record + "\n")
        except OSError as exc:
            raise GameMemoryError(f"could not write {game}/{RELOAD_REASONS_FILE}: {exc}") from exc

    def recall_reload_reasons(self, game: str, signature: StorySignature) -> list[dict[str, str]]:
        """Recall recorded reload reasons for a matching story build."""
        game_dir = self.game_dir(game)
        if not game_dir.exists():
            return []
        if not game_dir.is_dir():
            raise GameMemoryError(f"{game_dir.name} is not a game-memory directory")
        try:
            matches = self._matches(game_dir, game, signature)
        except GameMemoryError as exc:
            aside = self._rotate(game_dir)
            logger.warning(
                "Game memories in %s are unreadable (%s); moved them to %s",
                game_dir.name,
                exc,
                aside.name,
            )
            return []
        if not matches:
            aside = self._rotate(game_dir)
            logger.warning(
                "Game memories in %s belong to another build; moved them to %s",
                game_dir.name,
                aside.name,
            )
            return []
        return self._recall_reload_reasons(game_dir)

    def _prepare(self, game: str, signature: StorySignature) -> Path:
        """Return a matching game directory, creating or rotating as needed."""
        game_dir = self.game_dir(game)
        if game_dir.exists():
            if not game_dir.is_dir():
                raise GameMemoryError(f"{game_dir.name} is not a game-memory directory")
            if not self._matches(game_dir, game, signature):
                aside = self._rotate(game_dir)
                logger.warning(
                    "Game memories in %s belong to another build; moved them to %s",
                    game_dir.name,
                    aside.name,
                )

        if not game_dir.exists():
            game_dir.mkdir(parents=True)
            self._write_json(
                game_dir / GAME_FILE,
                {
                    "version": GAME_MEMORY_VERSION,
                    "game": game,
                    "story_signature": signature.as_record(),
                },
            )
        return game_dir

    @staticmethod
    def _recall_reload_reasons(game_dir: Path) -> list[dict[str, str]]:
        """Read and validate a game's append-only reload journal."""
        path = game_dir / RELOAD_REASONS_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise GameMemoryError(
                f"could not read {game_dir.name}/{RELOAD_REASONS_FILE}: {exc}"
            ) from exc

        reasons: list[dict[str, str]] = []
        try:
            for line in lines:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record is not an object")
                location = value.get("location")
                reason = value.get("reason")
                if not isinstance(location, str) or not location.strip():
                    raise ValueError("location is not a non-empty string")
                if not isinstance(reason, str) or not reason.strip():
                    raise ValueError("reason is not a non-empty string")
                reasons.append({"location": location, "reason": reason})
        except (json.JSONDecodeError, ValueError) as exc:
            raise GameMemoryError(
                f"{game_dir.name}/{RELOAD_REASONS_FILE} is unreadable: {exc}"
            ) from exc
        return reasons

    @staticmethod
    def _matches(game_dir: Path, game: str, signature: StorySignature) -> bool:
        path = game_dir / GAME_FILE
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GameMemoryError(f"{game_dir.name} has no {GAME_FILE}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GameMemoryError(f"could not read {game_dir.name}/{GAME_FILE}: {exc}") from exc
        try:
            if not isinstance(value, dict):
                raise ValueError("top level is not an object")
            if value.get("version") != GAME_MEMORY_VERSION:
                raise ValueError(f"unsupported version {value.get('version')!r}")
            if value.get("game") != game:
                raise ValueError(f"claims to belong to {value.get('game')!r}")
            stored_signature = StorySignature.from_record(value.get("story_signature"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GameMemoryError(f"{game_dir.name}/{GAME_FILE} is unreadable: {exc}") from exc
        return stored_signature == signature

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(f"{path.suffix}.part")
        try:
            temporary.write_text(
                json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise GameMemoryError(f"could not write {path}: {exc}") from exc

    def _rotate(self, game_dir: Path) -> Path:
        for number in range(1, 10_000):
            aside = self.dir / f"{game_dir.name}.{number:04d}"
            if not aside.exists():
                game_dir.rename(aside)
                return aside
        raise GameMemoryError(f"nowhere left to move incompatible {game_dir.name}")


def merge_maps(destination: KnownMap, source: KnownMap) -> bool:
    """Add every route in *source* to *destination*, returning whether it grew."""
    changed = False
    for origin, routes in source.items():
        known_routes = destination.setdefault(origin, {})
        for target, command in routes.items():
            if known_routes.get(target) == command:
                continue
            known_routes[target] = command
            changed = True
    return changed
