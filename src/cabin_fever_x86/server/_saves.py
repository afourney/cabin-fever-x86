"""Saved games for the machine in the corner.

Jericho does not hand its state out as bytes: :meth:`FrotzEnv.get_state`
returns a tuple of ``(ram, stack, pc, sp, fp, frame_count, opcode, rng,
narrative)`` — two numpy ``uint8`` arrays, six integers, a triple of them, and
the last thing the game printed. All of it writes down flat, so a save is a
JSON header and the three byte runs it describes::

    CFX86SAVE1
    {"game": "zork1", "score": 10, "pc": 22797, "ram_size": 11859, ...}
    <ram bytes><stack bytes><narrative bytes>

Spelled out rather than pickled: a save is data, and nothing read back off the
disk should be able to run code. It also means a listing costs two lines per
file rather than a full decode, a save can be read with ``head -2``, and a
change to Jericho's tuple fails loudly here instead of quietly restoring
nonsense.

The header also carries the screen as it was left. ``set_state`` restores the
interpreter but tells us nothing about what was last printed, and a machine
that comes back showing the wrong text is worse than one that will not come
back at all.

Every save belongs to one session and sits under that session's server folder.
A numbered save is named for the game it came out of — ``zork1_0003.bin`` — so a
folder can be read at a glance. Each game counts from ``0001`` up to ``9999`` on
its own, so the game and the number together are what name a save.

The autosave is the exception: one ``autosave.bin``, rewritten after every move,
holding whatever was last in the drive. It is the machine's resume point, not a
save anyone asked for, so it is never listed among them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np

logger = logging.getLogger(__name__)

SAVES_DIR = "saves"
SUFFIX = ".bin"

# Rewritten after every move; the one save that is always there to come back to.
AUTOSAVE = "autosave"

# Numbered saves run 0001 to 9999. A session that fills all of them has been a
# long night.
FIRST_SLOT = 1
LAST_SLOT = 9999

# What a numbered save is called: the game, an underscore, four digits. The game
# is matched greedily so a ROM with an underscore of its own still parses, and
# checked against _SAFE_GAME before it is ever joined to a path.
_NUMBERED = re.compile(r"^(?P<game>.+)_(?P<slot>[0-9]{1,4})$")
_SAFE_GAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# First line of every file, so a file that is not one of ours — or is from a
# format we no longer read — is refused before anything is made of its bytes.
MAGIC = b"CFX86SAVE1"

# Jericho's state tuple, in the order get_state() returns it and set_state()
# unpacks it. The two buffers and the rng triple are handled on their own; the
# rest are plain integers that go straight into the header.
STATE_LENGTH = 9
SCALARS = ("pc", "sp", "fp", "frame_count", "opcode")


class SaveError(Exception):
    """A save could not be written, found, or read back."""


@dataclass(frozen=True)
class Snapshot:
    """One moment on the machine: the interpreter's state, and the screen with it."""

    game: str
    state: tuple[Any, ...]
    observation: str
    score: int | None
    moves: int | None
    done: bool


@dataclass(frozen=True)
class SaveInfo:
    """What a save says about itself, read from its header alone."""

    name: str
    game: str
    score: int | None
    moves: int | None
    done: bool
    saved_at: datetime

    def describe(self) -> str:
        """One line, for a listing the companion can read out."""
        # The name already carries the game, so there is no need to say it twice.
        named_for_its_game = self.name.startswith(f"{self.game}_")
        parts = [self.name if named_for_its_game else f"{self.name}: {self.game}"]
        if self.score is not None:
            parts.append(f"score {self.score}")
        if self.moves is not None:
            parts.append(f"{self.moves} move" if self.moves == 1 else f"{self.moves} moves")
        if self.done:
            parts.append("game over")
        parts.append(self.saved_at.astimezone().strftime("%H:%M:%S"))
        return ", ".join(parts)


def parse_name(name: str) -> tuple[str, int] | None:
    """Read a save name into ``("zork1", 3)``, or ``None`` for the autosave. Raises otherwise.

    Forgiving about what a caller will get wrong — the extension, the case, a
    slot written ``3`` rather than ``0003`` — and unforgiving about everything
    else, since both halves end up in a path. A number on its own is not a name:
    each game counts from one, so it would say nothing about which save is meant.
    """
    wanted = name.strip().casefold()
    if wanted.endswith(SUFFIX):
        wanted = wanted[: -len(SUFFIX)]
    if wanted == AUTOSAVE:
        return None

    found = _NUMBERED.match(wanted)
    if found:
        slot, game = int(found["slot"]), found["game"]
        if FIRST_SLOT <= slot <= LAST_SLOT and _SAFE_GAME.match(game):
            return game, slot

    raise SaveError(
        f"{name.strip()!r} is not a save name. A save is named for its game and "
        f"numbered, like 'zork1_{FIRST_SLOT:04d}'."
    )


class SaveStore:
    """The saves folder for one session."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        """Point at a folder. It is created on the first write, not now."""
        self.dir = Path(directory)

    def path(self, name: str) -> Path:
        """Where the save called *name* lives."""
        wanted = parse_name(name)
        if wanted is None:
            return self.dir / f"{AUTOSAVE}{SUFFIX}"
        game, slot = wanted
        return self.dir / f"{game}_{slot:04d}{SUFFIX}"

    def next_name(self, game: str) -> str:
        """Pick *game*'s next unused slot.

        Each game counts from one. Counting up from its highest slot on disk
        rather than from how many it has, so a deleted save leaves a gap instead
        of a collision.
        """
        if not _SAFE_GAME.match(game):
            raise SaveError(f"{game!r} is not a name a save can be filed under")

        highest = 0
        for path in self.dir.glob(f"{game}_{'[0-9]' * 4}{SUFFIX}"):
            _game, _, digits = path.stem.rpartition("_")
            with contextlib.suppress(ValueError):
                highest = max(highest, int(digits))
        slot = max(highest + 1, FIRST_SLOT)
        if slot > LAST_SLOT:
            raise SaveError(f"All {LAST_SLOT} of {game}'s save slots are full.")
        return f"{game}_{slot:04d}"

    def write(self, name: str, snapshot: Snapshot) -> str:
        """Write one save and return the name it went under.

        Written beside the real file and moved into place, so an interrupted
        save cannot leave a half-written autosave where a good one used to be.
        """
        path = self.path(name)
        header, body = _encode_state(snapshot.state)
        header |= {
            "game": snapshot.game,
            "score": snapshot.score,
            "moves": snapshot.moves,
            "done": snapshot.done,
            "saved_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "observation": snapshot.observation,
        }

        self.dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{SUFFIX}.part")
        try:
            with temporary.open("wb") as handle:
                handle.write(MAGIC + b"\n")
                handle.write(json.dumps(header).encode("utf-8") + b"\n")
                handle.write(body)
            os.replace(temporary, path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise SaveError(f"could not write {path.name}: {exc}") from exc
        return path.stem

    def read(self, name: str) -> Snapshot:
        """Read one save back, screen and all."""
        path = self.path(name)
        try:
            with path.open("rb") as handle:
                header = _read_header(handle, path)
                state = _decode_state(header, handle, path)
        except FileNotFoundError as exc:
            raise SaveError(f"there is no save called {path.stem!r}") from exc
        except OSError as exc:
            raise SaveError(f"{path.name} will not read back: {exc}") from exc

        return Snapshot(
            game=str(header.get("game") or ""),
            state=state,
            observation=str(header.get("observation") or ""),
            score=header.get("score"),
            moves=header.get("moves"),
            done=bool(header.get("done")),
        )

    def info(self, name: str) -> SaveInfo:
        """Read one save's header, without the state behind it."""
        path = self.path(name)
        try:
            with path.open("rb") as handle:
                header = _read_header(handle, path)
        except FileNotFoundError as exc:
            raise SaveError(f"there is no save called {path.stem!r}") from exc
        except OSError as exc:
            raise SaveError(f"{path.name} will not read back: {exc}") from exc
        return _info(path.stem, header)

    def list(self) -> list[SaveInfo]:
        """Every readable numbered save, in slot order.

        The autosave is not one of them. It is the machine's own resume point,
        not a save anyone chose to make, and it is reached by name — see
        :meth:`read` — rather than by turning up in a listing.

        A file that will not parse is left out with a warning rather than taking
        the whole listing down with it.
        """
        if not self.dir.is_dir():
            return []

        found: list[SaveInfo] = []
        for path in sorted(self.dir.glob(f"*_{'[0-9]' * 4}{SUFFIX}")):
            try:
                found.append(self.info(path.stem))
            except SaveError as exc:
                logger.warning("Skipping unreadable save %s: %s", path.name, exc)
        return found


def _encode_state(state: tuple[Any, ...]) -> tuple[dict[str, Any], bytes]:
    """Flatten Jericho's state tuple into a header and the bytes behind it."""
    if len(state) != STATE_LENGTH:
        raise SaveError(
            f"expected {STATE_LENGTH} fields of machine state, got {len(state)}; "
            "this build of Jericho keeps its state differently"
        )
    ram, stack, *scalars, rng, narrative = state
    ram, stack = bytes(bytearray(ram)), bytes(bytearray(stack))
    narrative = bytes(narrative or b"")

    header: dict[str, Any] = dict(zip(SCALARS, (int(value) for value in scalars), strict=True))
    header |= {
        "rng": [int(value) for value in rng],
        "ram_size": len(ram),
        "stack_size": len(stack),
        "narrative_size": len(narrative),
    }
    return header, ram + stack + narrative


def _decode_state(header: dict[str, Any], handle: BinaryIO, path: Path) -> tuple[Any, ...]:
    """Rebuild the state tuple ``set_state`` wants from a header and an open file.

    The buffers come back as writeable arrays because ``set_state`` hands them
    to ctypes, which refuses a read-only view of a ``bytes``.
    """
    try:
        scalars = [int(header[name]) for name in SCALARS]
        rng = tuple(int(value) for value in header["rng"])
        sizes = [int(header[name]) for name in ("ram_size", "stack_size", "narrative_size")]
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"{path.name} is missing part of its machine state: {exc}") from exc
    if len(rng) != 3:
        raise SaveError(f"{path.name} has an unreadable random seed")

    runs: list[bytes] = []
    for size in sizes:
        run = handle.read(size)
        if len(run) != size:
            raise SaveError(f"{path.name} is truncated: it stops partway through the state")
        runs.append(run)
    ram, stack, narrative = runs

    return (_buffer(ram), _buffer(stack), *scalars, rng, narrative)


def _buffer(run: bytes) -> np.ndarray[Any, np.dtype[np.uint8]]:
    """Wrap *run* in a writeable ``uint8`` array, which is what ctypes will take."""
    return np.frombuffer(bytearray(run), dtype=np.uint8)


def _read_header(handle: BinaryIO, path: Path) -> dict[str, Any]:
    """Read the magic line and the header off an open save."""
    if handle.readline().rstrip(b"\n") != MAGIC:
        raise SaveError(f"{path.name} is not a save from this machine")
    try:
        header = json.loads(handle.readline().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SaveError(f"{path.name} has an unreadable header: {exc}") from exc
    if not isinstance(header, dict):
        raise SaveError(f"{path.name} has an unreadable header")
    return header


def _info(name: str, header: dict[str, Any]) -> SaveInfo:
    """Build a listing entry out of a header, forgiving a missing timestamp."""
    stamp = header.get("saved_at")
    try:
        saved_at = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        saved_at = datetime.fromtimestamp(0, tz=UTC)
    return SaveInfo(
        name=name,
        game=str(header.get("game") or "unknown"),
        score=header.get("score"),
        moves=header.get("moves"),
        done=bool(header.get("done")),
        saved_at=saved_at,
    )
