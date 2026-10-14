"""Saved games for the machine in the corner.

Fernz checkpoints are immutable dataclasses. We store their execution fields
as JSON and dynamic memory as a byte run, without pickle or executable data::

    CFX86SAVE3
    {"type": "metadata", "game": "zork1", "checkpoint": {...}, ...}
    {"type": "personal_map", "edges": [...]}
    CFX86DATA
    <ram bytes>

The format is tied to Fernz's schema-5 checkpoint. Jericho's CFX86SAVE2 files
can be imported at supported V3 line-input boundaries.

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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
from fernz import GameState
from fernz._state import FrameState, InputRequest, OutputState, RandomState, StoredInputRequest

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
MAGIC = b"CFX86SAVE3"
LEGACY_MAGIC = b"CFX86SAVE2"
CHECKPOINT_VERSION = 5
DATA_SENTINEL = b"CFX86DATA"
METADATA = "metadata"
PERSONAL_MAP = "personal_map"
COMMENT_LIMIT = 500
COMMENT_PREVIEW = 120
Location = tuple[int, str]
RunMap = dict[Location, dict[Location, str]]
KnownMap = dict[Location, dict[Location, str]]
# Kept as a public alias while callers and old save records still use the
# original name. In a save, this is the map of the run at that restore point.
PersonalMap = RunMap


class SaveError(Exception):
    """A save could not be written, found, or read back."""


@dataclass(frozen=True)
class StorySignature:
    """The standard Z-machine identity fields from a story-file header."""

    release: int
    serial: str
    checksum: int

    def as_record(self) -> dict[str, int | str]:
        return {
            "release": self.release,
            "serial": self.serial,
            "checksum": self.checksum,
        }

    @classmethod
    def from_record(cls, value: Any) -> StorySignature:
        if not isinstance(value, dict):
            raise ValueError("not an object")
        serial = value.get("serial")
        if not isinstance(serial, str) or len(serial) != 6:
            raise ValueError("serial must be six characters")
        release = int(value["release"])
        checksum = int(value["checksum"])
        if not 0 <= release <= 0xFFFF or not 0 <= checksum <= 0xFFFF:
            raise ValueError("release and checksum must be unsigned 16-bit values")
        return cls(release=release, serial=serial, checksum=checksum)


@dataclass(frozen=True)
class Snapshot:
    """One moment on the machine: the interpreter's state, and the screen with it."""

    game: str
    state: GameState | tuple[Any, ...]
    observation: str
    score: int | None
    moves: int | None
    done: bool
    location: str | None = None
    comment: str | None = None
    personal_map: PersonalMap | None = None
    # Optional listing metadata; Fernz always validates its own story hash.
    story_signature: StorySignature | None = None


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

    def _quetzal_path(self, game: str) -> Path:
        """Keep built-in saves beside this session's checkpoints, one per ROM."""
        if not _SAFE_GAME.fullmatch(game):
            raise SaveError(f"{game!r} is not a name a save can be filed under")
        return self.dir / f"{game}.qzl"

    def write_quetzal(self, game: str, data: bytes) -> bool:
        """Handle a story's save opcode, replacing its Quetzal file atomically."""
        temporary: Path | None = None
        try:
            path = self._quetzal_path(game)
            self.dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".qzl.part")
            temporary.write_bytes(data)
            os.replace(temporary, path)
        except (OSError, SaveError) as exc:
            logger.warning("Could not write Quetzal save for %s: %s", game, exc)
            return False
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
        return True

    def read_quetzal(self, game: str) -> bytes | None:
        """Handle a story's restore opcode; absent or unreadable saves fail normally."""
        try:
            return self._quetzal_path(game).read_bytes()
        except FileNotFoundError:
            return None
        except (OSError, SaveError) as exc:
            logger.warning("Could not read Quetzal save for %s: %s", game, exc)
            return None

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
        if snapshot.story_signature is not None:
            metadata["story_signature"] = snapshot.story_signature.as_record()
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
                magic, metadata, records = _read_records(handle, path)
                state = (
                    _decode_legacy_state(metadata, handle, path)
                    if magic == LEGACY_MAGIC
                    else _decode_state(metadata, handle, path)
                )
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
            story_signature=_optional_story_signature(metadata, path),
        )

    def info(self, name: str) -> SaveInfo:
        """Read one save's header, without the state behind it."""
        path = self.path(name)
        try:
            with path.open("rb") as handle:
                _magic, metadata = _read_metadata(handle, path)
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


def _encode_state(state: GameState) -> tuple[dict[str, Any], bytes]:
    """Flatten the pinned Fernz checkpoint schema without executable serialization."""
    if not isinstance(state, GameState) or state.format_version != CHECKPOINT_VERSION:
        raise SaveError("expected a Fernz schema-5 checkpoint")
    checkpoint = asdict(state)
    ram = checkpoint.pop("ram")
    return {"checkpoint": checkpoint, "ram_size": len(ram)}, ram


def _decode_state(header: dict[str, Any], handle: BinaryIO, path: Path) -> GameState:
    """Rebuild immutable checkpoint records; Fernz validates them against the story."""
    try:
        size = header["ram_size"]
        checkpoint = dict(header["checkpoint"])
        if type(size) is not int or size < 0:
            raise ValueError("invalid RAM size")
        if checkpoint["format_version"] != CHECKPOINT_VERSION:
            raise ValueError("unsupported Fernz checkpoint version")
        ram = handle.read(size)
        if len(ram) != size:
            raise SaveError(f"{path.name} is truncated: it stops partway through the state")
        if handle.read(1):
            raise ValueError("unexpected bytes after the checkpoint")
        frames = tuple(
            FrameState(
                **(frame | {"locals": tuple(frame["locals"]), "stack": tuple(frame["stack"])})
            )
            for frame in checkpoint.pop("frames")
        )
        rng = checkpoint.pop("rng")
        if rng["python_state"] is not None:
            version, words, gaussian = rng["python_state"]
            rng["python_state"] = (version, tuple(words), gaussian)
        request = checkpoint.pop("input_request")
        if request is not None:
            request = (
                StoredInputRequest(**request) if "store" in request else InputRequest(**request)
            )
        output = checkpoint.pop("output")
        for key in ("cursors", "colours", "memory_streams"):
            output[key] = tuple(tuple(pair) for pair in output[key])
        for key in ("fonts", "styles"):
            output[key] = tuple(output[key])
        return GameState(
            **checkpoint,
            ram=ram,
            frames=frames,
            rng=RandomState(**rng),
            input_request=request,
            output=OutputState(**output),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise SaveError(
            f"{path.name} is missing part of its machine state or is invalid: {exc}"
        ) from exc


def _decode_legacy_state(header: dict[str, Any], handle: BinaryIO, path: Path) -> tuple[Any, ...]:
    """Read the original CFX86SAVE2 tuple for the measured V3 converter."""
    try:
        scalars = [header[key] for key in ("pc", "sp", "fp", "frame_count", "opcode")]
        rng = tuple(header["rng"])
        sizes = [header[key] for key in ("ram_size", "stack_size", "narrative_size")]
        if any(type(value) is not int for value in (*scalars, *rng, *sizes)):
            raise ValueError("state fields must be integers")
        if len(rng) != 3 or any(size < 0 for size in sizes):
            raise ValueError("invalid RNG or buffer lengths")
        body = handle.read()
        if len(body) != sum(sizes):
            raise ValueError("truncated state or unexpected trailing bytes")
        ram_size, stack_size, _ = sizes
        ram = np.frombuffer(body[:ram_size], dtype=np.uint8).copy()
        stack = np.frombuffer(body[ram_size : ram_size + stack_size], dtype=np.uint8).copy()
        narrative = body[ram_size + stack_size :]
        return ram, stack, *scalars, rng, narrative
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"{path.name} has an invalid legacy machine state: {exc}") from exc


def _read_metadata(handle: BinaryIO, path: Path) -> tuple[bytes, dict[str, Any]]:
    """Read the format magic and first, listing-friendly metadata record."""
    magic = handle.readline().rstrip(b"\n")
    if magic not in (MAGIC, LEGACY_MAGIC):
        raise SaveError(f"{path.name} is not a save from this machine")
    record = _read_json_record(handle.readline(), path)
    if record.get("type") != METADATA:
        raise SaveError(f"{path.name} has no metadata record")
    return magic, record


def _read_records(
    handle: BinaryIO, path: Path
) -> tuple[bytes, dict[str, Any], dict[str, dict[str, Any]]]:
    """Read typed records through the data sentinel, skipping unknown types."""
    magic, metadata = _read_metadata(handle, path)
    records = {METADATA: metadata}
    while True:
        line = handle.readline()
        if not line:
            raise SaveError(f"{path.name} has no {DATA_SENTINEL.decode()} marker")
        if line.rstrip(b"\n") == DATA_SENTINEL:
            return magic, metadata, records
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
        raise SaveError(f"{path.name} has an unreadable record: {exc}") from exc
    if not isinstance(record, dict):
        raise SaveError(f"{path.name} has an unreadable record")
    return record


def _json_line(record: dict[str, Any]) -> bytes:
    """Encode one compact physical JSON record line."""
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _encode_map(personal_map: PersonalMap) -> dict[str, Any]:
    """Turn tuple-keyed directed routes into JSON records."""
    return {"type": PERSONAL_MAP, "version": 1, "edges": encode_map_edges(personal_map)}


def encode_map_edges(personal_map: PersonalMap) -> list[dict[str, Any]]:
    """Turn a tuple-keyed map into portable JSON edge records."""
    edges: list[dict[str, Any]] = []
    for source, destinations in personal_map.items():
        for destination, command in destinations.items():
            edges.append({"from": list(source), "to": list(destination), "command": command})
    return edges


def _decode_map(record: dict[str, Any] | None, path: Path) -> PersonalMap:
    """Rebuild a tuple-keyed map, accepting a missing optional record."""
    if record is None:
        return {}
    if record.get("version") != 1 or not isinstance(record.get("edges"), list):
        raise SaveError(f"{path.name} has an unreadable personal map")
    try:
        return decode_map_edges(record["edges"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"{path.name} has an unreadable personal map: {exc}") from exc


def decode_map_edges(edges: Any) -> PersonalMap:
    """Validate portable JSON edge records and rebuild a tuple-keyed map."""
    if not isinstance(edges, list):
        raise ValueError("edges must be a list")
    personal_map: PersonalMap = {}
    for edge in edges:
        source = _decode_location(edge["from"])
        destination = _decode_location(edge["to"])
        command = str(edge["command"])
        personal_map.setdefault(source, {})[destination] = command
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


def _optional_story_signature(metadata: dict[str, Any], path: Path) -> StorySignature | None:
    """Read an optional signature, accepting old saves that have none."""
    if "story_signature" not in metadata:
        return None
    try:
        return StorySignature.from_record(metadata["story_signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SaveError(f"{path.name} has an unreadable story signature: {exc}") from exc


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
