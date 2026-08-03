"""Saved games for the machine in the corner.

Jericho does not hand its state out as bytes: :meth:`FrotzEnv.get_state`
returns a tuple of ``(ram, stack, pc, sp, fp, frame_count, opcode, rng,
narrative)`` — two numpy ``uint8`` arrays, six integers, a triple of them, and
the last thing the game printed. All of it writes down flat, so a save is typed
JSON records and the three byte runs they describe::

    CFX86SAVE2
    {"type": "metadata", "game": "zork1", "score": 10, ...}
    {"type": "personal_map", "edges": [...]}
    CFX86DATA
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
MAGIC = b"CFX86SAVE2"
DATA_SENTINEL = b"CFX86DATA"
METADATA = "metadata"
PERSONAL_MAP = "personal_map"
COMMENT_LIMIT = 500
COMMENT_PREVIEW = 120
Location = tuple[int, str]
PersonalMap = dict[Location, dict[Location, str]]

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
    location: str | None = None
    comment: str | None = None
    personal_map: PersonalMap | None = None


@dataclass(frozen=True)
class SaveInfo:
    """What a save says about itself, read from its header alone."""

    name: str
    game: str
    score: int | None
    moves: int | None
    done: bool
    saved_at: datetime
    location: str | None
    comment: str | None

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
        if self.location:
            parts.append(f"location {self.location}")
        if self.comment:
            parts.append(f"note: {_preview(self.comment)}")
        parts.append(self.saved_at.astimezone().strftime("%H:%M:%S"))
        return ", ".join(parts)

    def describe_full(self) -> str:
        """All metadata for one save, including its complete comment."""
        parts = [f"Save: {self.name}", f"Game: {self.game}"]
        if self.score is not None:
            parts.append(f"Score: {self.score}")
        if self.moves is not None:
            parts.append(f"Moves: {self.moves}")
        if self.done:
            parts.append("Game over: yes")
        if self.location:
            parts.append(f"Location: {self.location}")
        parts.append(f"Saved: {self.saved_at.astimezone().isoformat(timespec='seconds')}")
        if self.comment:
            parts.append(f"Comment: {self.comment}")
        return "\n".join(parts)


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
        metadata, body = _encode_state(snapshot.state)
        comment = _comment(snapshot.comment)
        metadata |= {
            "type": METADATA,
            "game": snapshot.game,
            "score": snapshot.score,
            "moves": snapshot.moves,
            "done": snapshot.done,
            "saved_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "observation": snapshot.observation,
        }
        if snapshot.location:
            metadata["location"] = snapshot.location
        if comment:
            metadata["comment"] = comment
        map_record = _encode_map(snapshot.personal_map or {})

        self.dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{SUFFIX}.part")
        try:
            with temporary.open("wb") as handle:
                handle.write(MAGIC + b"\n")
                handle.write(_json_line(metadata))
                handle.write(_json_line(map_record))
                handle.write(DATA_SENTINEL + b"\n")
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
                metadata, records = _read_records(handle, path)
                state = _decode_state(metadata, handle, path)
        except FileNotFoundError as exc:
            raise SaveError(f"there is no save called {path.stem!r}") from exc
        except OSError as exc:
            raise SaveError(f"{path.name} will not read back: {exc}") from exc

        return Snapshot(
            game=str(metadata.get("game") or ""),
            state=state,
            observation=str(metadata.get("observation") or ""),
            score=metadata.get("score"),
            moves=metadata.get("moves"),
            done=bool(metadata.get("done")),
            location=_optional_text(metadata.get("location")),
            comment=_optional_text(metadata.get("comment")),
            personal_map=_decode_map(records.get(PERSONAL_MAP), path),
        )

    def info(self, name: str) -> SaveInfo:
        """Read one save's header, without the state behind it."""
        path = self.path(name)
        try:
            with path.open("rb") as handle:
                metadata = _read_metadata(handle, path)
        except FileNotFoundError as exc:
            raise SaveError(f"there is no save called {path.stem!r}") from exc
        except OSError as exc:
            raise SaveError(f"{path.name} will not read back: {exc}") from exc
        return _info(path.stem, metadata)

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


def _read_metadata(handle: BinaryIO, path: Path) -> dict[str, Any]:
    """Read the magic and first, listing-friendly metadata record."""
    if handle.readline().rstrip(b"\n") != MAGIC:
        raise SaveError(f"{path.name} is not a save from this machine")
    record = _read_json_record(handle.readline(), path)
    if record.get("type") != METADATA:
        raise SaveError(f"{path.name} has no metadata record")
    return record


def _read_records(handle: BinaryIO, path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read typed records through the data sentinel, skipping unknown types."""
    metadata = _read_metadata(handle, path)
    records = {METADATA: metadata}
    while True:
        line = handle.readline()
        if not line:
            raise SaveError(f"{path.name} has no {DATA_SENTINEL.decode()} marker")
        if line.rstrip(b"\n") == DATA_SENTINEL:
            return metadata, records
        record = _read_json_record(line, path)
        record_type = record.get("type")
        if not isinstance(record_type, str):
            raise SaveError(f"{path.name} has a record without a type")
        if record_type in records:
            raise SaveError(f"{path.name} has more than one {record_type!r} record")
        if record_type == PERSONAL_MAP:
            records[record_type] = record


def _read_json_record(line: bytes, path: Path) -> dict[str, Any]:
    """Decode one physical JSON record line."""
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SaveError(f"{path.name} has an unreadable header: {exc}") from exc
    if not isinstance(record, dict):
        raise SaveError(f"{path.name} has an unreadable header")
    return record


def _json_line(record: dict[str, Any]) -> bytes:
    """Encode one compact physical JSON record line."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _encode_map(personal_map: PersonalMap) -> dict[str, Any]:
    """Turn tuple-keyed directed routes into JSON records."""
    edges = []
    for source, destinations in personal_map.items():
        for destination, command in destinations.items():
            edges.append({"from": list(source), "to": list(destination), "command": command})
    return {"type": PERSONAL_MAP, "version": 1, "edges": edges}


def _decode_map(record: dict[str, Any] | None, path: Path) -> PersonalMap:
    """Rebuild a tuple-keyed map, accepting a missing optional record."""
    if record is None:
        return {}
    if record.get("version") != 1 or not isinstance(record.get("edges"), list):
        raise SaveError(f"{path.name} has an unreadable personal map")
    personal_map: PersonalMap = {}
    try:
        for edge in record["edges"]:
            source = _decode_location(edge["from"])
            destination = _decode_location(edge["to"])
            command = str(edge["command"])
            personal_map.setdefault(source, {})[destination] = command
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"{path.name} has an unreadable personal map: {exc}") from exc
    return personal_map


def _decode_location(value: Any) -> Location:
    """Validate one ``[object number, room name]`` JSON location."""
    if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        raise ValueError("bad location")
    return int(value[0]), value[1]


def _comment(value: str | None) -> str | None:
    """Normalize and bound an optional save comment."""
    if value is None:
        return None
    comment = value.strip()
    if len(comment) > COMMENT_LIMIT:
        raise SaveError(f"a save comment cannot exceed {COMMENT_LIMIT} characters")
    return comment or None


def _optional_text(value: Any) -> str | None:
    """Return a nonempty metadata string or no value."""
    return value if isinstance(value, str) and value else None


def _preview(comment: str) -> str:
    """Collapse whitespace and shorten a comment for a multi-save listing."""
    line = " ".join(comment.split())
    if len(line) <= COMMENT_PREVIEW:
        return line
    return f"{line[: COMMENT_PREVIEW - 1].rstrip()}…"


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
        location=_optional_text(header.get("location")),
        comment=_optional_text(header.get("comment")),
    )
