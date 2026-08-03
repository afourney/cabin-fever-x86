#!/usr/bin/env python3
"""Play a Z-machine game or Cabin Fever save through Jericho directly.

    uv run play_jericho.py data/games/zork1.z5
    uv run play_jericho.py data/sessions/<session>/server/saves/autosave.bin

Every line entered at the prompt is passed unchanged to ``FrotzEnv.step``.
Press Ctrl-D (or Ctrl-C) to leave the console.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from jericho import FrotzEnv

from cabin_fever_x86_core.server._saves import SaveError, SaveStore

DEFAULT_GAMES_DIR = Path("data/games")
GAME_SUFFIXES = frozenset(f".z{version}" for version in range(3, 9))
Location = tuple[int, str]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="A .z3-.z8 game or Cabin Fever .bin save.")
    parser.add_argument(
        "--games-dir",
        type=Path,
        default=DEFAULT_GAMES_DIR,
        help=f"Where saved games' ROMs live (default: {DEFAULT_GAMES_DIR}).",
    )
    return parser.parse_args(argv)


def _find_game(games_dir: Path, game: str) -> Path:
    """Find the ROM named by a snapshot, ignoring filename case."""
    matches = [
        path for path in games_dir.glob("*.z[3-8]") if path.stem.casefold() == game.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(f"could not find exactly one ROM for {game!r} under {games_dir}")
    return matches[0]


def _load(path: Path, games_dir: Path) -> tuple[FrotzEnv, str, dict[str, Any]]:
    """Start a ROM, optionally restoring the Jericho state in a Cabin Fever save."""
    suffix = path.suffix.casefold()
    if suffix in GAME_SUFFIXES:
        env = FrotzEnv(str(path))
        observation, info = env.reset()
        return env, observation, dict(info)

    if suffix != ".bin":
        raise ValueError(f"{path} is not a .z3-.z8 game or .bin save")

    snapshot = SaveStore(path.parent).read(path.stem)
    game_path = _find_game(games_dir, snapshot.game)
    env = FrotzEnv(str(game_path))
    try:
        env.reset()
        expected = len(snapshot.state[0])
        actual = env.frotz_lib.getRAMSize()
        if expected != actual:
            raise ValueError(
                f"save expects {expected} bytes of RAM, but {game_path.name} has {actual}"
            )
        env.set_state(snapshot.state)
    except Exception:
        env.close()
        raise
    info = {"score": env.get_score(), "moves": env.get_moves()}
    return env, snapshot.observation, info


def _show(observation: str, *, reward: int | None, done: bool, info: dict[str, Any]) -> None:
    """Show Jericho's complete response to reset or step."""
    print(observation.rstrip())
    fields = []
    if reward is not None:
        fields.append(f"reward={reward!r}")
    fields.extend((f"done={done!r}", f"info={info!r}"))
    print(f"\n[jericho: {', '.join(fields)}]")


def _location(env: FrotzEnv) -> Location | None:
    """Return the player's location when Jericho supports the game's object tree."""
    try:
        location = env.get_player_location()
    except (AttributeError, RuntimeError):
        return None
    if location is None:
        return None
    return location.num, location.name


def main(argv: list[str] | None = None) -> int:
    """Load the requested input and run the direct Jericho console loop."""
    args = _parse_args(argv)

    # Keep a personal map of the game world.
    # We identify each location by a tuple (num, name).

    # The structure is:
    # dict[ location, dict[location, str]]
    # Like: map[source_location][destination_location] = command_to_get_there
    # E.g., map[(180, "West of House")][(181, "North of House")] = "north"

    personal_map: dict[Location, dict[Location, str]] = {}

    try:
        env, observation, info = _load(args.path, args.games_dir)
    except (OSError, SaveError, ValueError, RuntimeError) as exc:
        print(f"play_jericho: {exc}", file=sys.stderr)
        return 2

    try:
        location = _location(env)
        _show(observation, reward=None, done=env.game_over() or env.victory(), info=info)
        while True:
            try:
                command = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            observation, reward, done, info = env.step(command)
            _show(observation, reward=reward, done=bool(done), info=dict(info))

            new_location = _location(env) if location is not None else None
            if new_location is not None and new_location != location:
                # We have moved to a new location. Update the map.
                personal_map.setdefault(location, {})[new_location] = command

                # Update current location
                location = new_location

            # Print the current map if we've got one:
            if location in personal_map:
                print("Personal map, from here:")
                for dest, cmd in personal_map[location].items():
                    print(f"- Type '{cmd}' to move to the location '{dest[1]}'")

            if done:
                break
    finally:
        env.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
