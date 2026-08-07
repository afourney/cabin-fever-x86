"""The beige PC in the corner of the cabin.

Wraps one Jericho interpreter: which game is in the drive, what it last put on
the screen, and the score. Nothing is running until a game is typed in at the
prompt, and a reboot puts it back that way.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from enum import Enum
from pathlib import Path
from typing import Any

from jericho import FrotzEnv

from cabin_fever_x86_core.hints import has_hints
from cabin_fever_x86_core.server._game_memories import (
    GameMemoryError,
    GameMemoryStore,
    merge_maps,
)
from cabin_fever_x86_core.server._saves import (
    AUTOSAVE,
    KnownMap,
    Location,
    RunMap,
    SaveError,
    SaveInfo,
    SaveStore,
    Snapshot,
    StorySignature,
)
from cabin_fever_x86_core.server._tools import (
    ListGamesTool,
    ListSavedGamesTool,
    LoadGameTool,
    NewGameTool,
    RebootTool,
    SaveGameTool,
)

logger = logging.getLogger(__name__)

GAMES_DIR = Path("data/games")

# Fixed fields in every Z-machine story-file header. Together these are the
# conventional identity used by Quetzal saves to distinguish story builds.
_RELEASE = slice(0x02, 0x04)
_SERIAL = slice(0x12, 0x18)
_CHECKSUM = slice(0x1C, 0x1E)
_HEADER_NEEDED = 0x1E


class VisitKind(Enum):
    """How familiar the current room was before the move that entered it."""

    REVISIT_THIS_RUN = "revisit_this_run"
    FIRST_THIS_RUN = "first_this_run"
    FIRST_EVER = "first_ever"


def _fresh_rng_a() -> int:
    """Return a portable, nonzero state for Frotz's normal RNG mode."""
    return secrets.randbelow(0x7FFFFFFF) + 1


NO_GAME = (
    "The computer is sitting at the DOS prompt. No game is running.\n"
    f"Use your `{ListGamesTool.name}` tool to see what is on the disk, and "
    f"your `{NewGameTool.name}` tool to start one.\n"
    f"You can also list your saved games with the `{ListSavedGamesTool.name}` "
    f"tool, and resume one with the `{LoadGameTool.name}` tool."
)

GAME_OVER = "The game has ended. Reboot the machine to play something else."

NOTHING_TO_SAVE = "Nothing is running, so there is nothing to save."

HINTS_AVAILABLE = (
    "Printed walkthroughs and InvisiClues-style hint books are available for this game through "
    "`request_hint`. They are reference materials in the cabin, not something you remember. "
    "Do not consult them unless the operator explicitly asks for a hint or agrees to look one up."
)
HINTS_UNAVAILABLE = (
    "There are no walkthroughs or InvisiClues-style hint books available for this game. "
    "Do not call `request_hint`."
)

# Z-machine games have their own verbs for saving, restoring, and quitting.
# We want to intercept those and tell the agent to use the tools instead.
#
# Measured across zork1, advent, curses, balances, 905 and anchor: match the verb, never
# the whole line — Zork saves on "save here" too, "here" being one of its own noise words.
GAME_SAVE_VERBS = frozenset({"save"})
GAME_RESTORE_VERBS = frozenset({"restore"})
# `q` hangs on the confirm prompt exactly like `quit`; `die` does in the five Inform games.
GAME_STOP_VERBS = frozenset({"quit", "q", "restart", "die"})

USE_THE_TOOLS = (
    "This computer uses tools rather than z-machine verbs for game management. "
    "Here's how you reach them:\n"
    f"  {SaveGameTool.name}\n"
    f"  {ListSavedGamesTool.name}\n"
    f"  {LoadGameTool.name}\n"
    f"  {NewGameTool.name}\n"
    f"  {RebootTool.name}\n"
    "Nothing was typed at the game console and no time has passed, so the screen is "
    "still showing what it showed before. Nothing has been lost."
)


class Machine:
    """One computer, running at most one game at a time.

    Given a *saves* store, the machine writes a restore point after every line
    typed at it and can be put back to any save it has written. A separate
    *game_memories* store carries routes remembered across those restore points.
    """

    def __init__(
        self,
        games_dir: Path = GAMES_DIR,
        saves: SaveStore | None = None,
        game_memories: GameMemoryStore | None = None,
    ) -> None:
        self._games_dir = games_dir
        self._saves = saves
        self._game_memories = game_memories
        self._env: FrotzEnv | None = None
        self._game: str | None = None
        self._story_signature: StorySignature | None = None
        self._observation = ""
        self._info: dict[str, Any] = {}
        self._done = False
        self._location: Location | None = None
        self._run_map: RunMap = {}
        self._known_map: KnownMap = {}
        self._known_map_writable = True
        self._hint_availability_pending = False

    @property
    def game(self) -> str | None:
        """The game currently in the drive, if any."""
        return self._game

    def list_games(self) -> list[str]:
        """List the games on the disk, by name."""
        if not self._games_dir.is_dir():
            logger.warning("No games directory at %s", self._games_dir)
            return []
        return sorted(path.stem for path in self._games_dir.glob("*.z[0-9]"))

    def _find(self, name: str) -> Path | None:
        """Resolve what was typed to a game file, forgiving case and extension."""
        wanted = name.strip().casefold().removesuffix(".z3").removesuffix(".z5")
        wanted = wanted.removesuffix(".z4").removesuffix(".z8").strip()
        if not wanted:
            return None

        paths = sorted(self._games_dir.glob("*.z[0-9]")) if self._games_dir.is_dir() else []
        for path in paths:
            if path.stem.casefold() == wanted:
                return path

        matches = [path for path in paths if path.stem.casefold().startswith(wanted)]
        return matches[0] if len(matches) == 1 else None

    def screen(self) -> str:
        """Show what is on the screen right now, game name and all."""
        if self._game is None:
            return NO_GAME

        parts = [f"GAME: {self._game}"]
        score, moves = self._info.get("score"), self._info.get("moves")
        if score is not None and self._env is not None:
            parts.append(f"Score {score}/{self._env.get_max_score()}")
        if moves is not None:
            parts.append(f"Moves {moves}")
        header = " | ".join(parts)

        body = self._observation.strip() or "(the screen is blank)"
        if self._done:
            body = f"{body}\n\n{GAME_OVER}"
        return f"{header}\n\n{body}"

    def observe(self) -> tuple[str, str | None]:
        """Show the screen and consume any one-shot private boot reminder."""
        return self.screen(), self._take_hint_availability_remark()

    async def _boot(self, name: str) -> str:
        """Load a game and return its opening screen.

        The primitive the public ways in are built from: it power-cycles the
        machine but leaves no restore point, so :meth:`load` can bring a game up
        without the autosave following it there.
        """
        path = self._find(name)
        if path is None:
            available = ", ".join(self.list_games()) or "nothing"
            return f"Bad command or file name: {name.strip()!r}\nOn the disk: {available}"

        self.reboot()
        try:
            env = await asyncio.to_thread(FrotzEnv, str(path), seed=_fresh_rng_a())
            observation, info = await asyncio.to_thread(env.reset)
        except Exception as exc:
            logger.exception("Could not load %s", path)
            return f"The machine refuses to load {path.stem}: {exc}"

        self._env, self._game = env, path.stem
        self._story_signature = _story_signature(path)
        self._observation, self._info, self._done = observation, dict(info), False
        self._location = _player_location(env)
        self._run_map = {}
        self._known_map = self._load_known_map()
        self._hint_availability_pending = True
        logger.info("Loaded %s", path.stem)
        return self.screen()

    async def new_game(self, name: str) -> str:
        """Start *name* from the beginning, dropping whatever was running.

        The same loose name resolution as typing one in at the prompt, and the
        same restore point at move zero, but it does not matter what was in the
        drive first: :meth:`_boot` power-cycles the machine on the way.
        """
        screen = await self._boot(name)
        await self._autosave()
        return screen

    async def new_game_with_memories(self, name: str) -> tuple[str, str | None]:
        """Start a fresh run and recall lessons from earlier reloads."""
        content = await self.new_game(name)
        if self._game is None:
            return content, None
        return content, _join_remarks(
            self._take_hint_availability_remark(),
            await self._recall_reload_reasons(),
        )

    async def type_text(self, text: str) -> tuple[str, str | None]:
        """Type a line and return its screen plus any private contextual remarks."""
        text = text.strip()
        if not text:
            return self.screen(), None

        # Before anything reaches the interpreter, and whether or not a game is
        # running: at the DOS prompt these are not games either.
        instead = _instead_of_typing(text)
        if instead is not None:
            logger.info("Held back %r; that is what the save tools are for", text)
            return instead, None

        if self._env is None:
            # At the DOS prompt, a line is the name of a game to start.
            return await self.new_game_with_memories(text)
        if self._done:
            return self.screen(), None

        old_location = self._location
        try:
            observation, _reward, done, info = await asyncio.to_thread(self._env.step, text)
        except Exception as exc:
            logger.exception("%r failed on %s", text, self._game)
            return f"The machine locks up for a moment: {exc}", None

        self._observation, self._info, self._done = observation, dict(info), bool(done)
        self._location = await asyncio.to_thread(_player_location, self._env)
        moved = (
            old_location is not None
            and self._location is not None
            and self._location != old_location
        )
        if moved:
            visit = _classify_visit(self._location, self._run_map, self._known_map)
            self._run_map.setdefault(old_location, {})[self._location] = text
            self._known_map.setdefault(old_location, {})[self._location] = text
            await self._persist_known_map()
        await self._autosave()
        remarks = (
            _describe_routes(self._location, self._run_map, self._known_map, visit)
            if moved
            else None
        )
        return self.screen(), remarks

    async def save(self, name: str | None = None, comment: str | None = None) -> str:
        """Write a save and say what it was called.

        *name* is for the autosave and for tests; left out, the next numbered
        slot is chosen.
        """
        if self._saves is None:
            return "This machine has no disk to save to."
        if self._env is None:
            return NOTHING_TO_SAVE
        try:
            slot = name or await asyncio.to_thread(self._saves.next_name, self._game)
            written = await asyncio.to_thread(self._write, slot, comment)
        except SaveError as exc:
            logger.warning("Could not save %s: %s", self._game, exc)
            return f"The save failed: {exc}"
        logger.info("Saved %s to %s", self._game, written)
        return f"Saved as {written}."

    async def load(self, name: str) -> str:
        """Put the machine back to a save, booting the right game if need be."""
        content, _loaded = await self._load(name)
        return content

    async def load_from_tool(self, name: str, reason: str | None = None) -> tuple[str, str | None]:
        """Load for the companion, optionally remembering why."""
        previous_game = self._game
        previous_signature = self._story_signature
        previous_location = self._location[1] if self._location is not None else None

        content, loaded = await self._load(name)
        if not loaded:
            return content, None

        if (
            reason
            and previous_game is not None
            and previous_game == self._game
            and previous_signature is not None
            and previous_signature == self._story_signature
            and previous_location is not None
        ):
            await self._remember_reload_reason(
                previous_game,
                previous_signature,
                previous_location,
                reason,
            )
        return content, self._take_hint_availability_remark()

    def _take_hint_availability_remark(self) -> str | None:
        """Return the hint-book status once after a ROM successfully boots."""
        if not self._hint_availability_pending or self._game is None:
            return None
        self._hint_availability_pending = False
        return HINTS_AVAILABLE if has_hints(self._game) else HINTS_UNAVAILABLE

    async def _load(self, name: str) -> tuple[str, bool]:
        """Restore a save and report whether the restore completed."""
        if self._saves is None:
            return "This machine has no disk to read saves from.", False

        try:
            snapshot = await asyncio.to_thread(self._saves.read, name)
            # What it is actually called, so a save asked for by number is
            # reported back the way the listing shows it.
            filed = await asyncio.to_thread(self._saves.path, name)
        except SaveError as exc:
            return f"That save will not load: {exc}", False
        name = filed.stem

        # The state only means anything to the game it came out of, and handing
        # one game's RAM to another would corrupt the interpreter outright.
        if snapshot.game != self._game:
            booted = await self._boot(snapshot.game)
            if self._game != snapshot.game:
                return f"{name} is a save of {snapshot.game}, which will not load:\n{booted}", False

        env = self._env
        if env is None:
            return f"The machine will not come up to load {name}.", False

        # Old CFX86SAVE2 files do not carry this optional field and must remain
        # loadable. When it is present, validate the precise story build before
        # handing its RAM to the interpreter.
        if (
            snapshot.story_signature is not None
            and snapshot.story_signature != self._story_signature
        ):
            logger.warning(
                "Save %s belongs to story %r, loaded %r",
                name,
                snapshot.story_signature,
                self._story_signature,
            )
            return (
                (
                    f"That save was written by a different build of {snapshot.game} "
                    "and will not load on this one."
                ),
                False,
            )

        expected = len(snapshot.state[0])
        actual = await asyncio.to_thread(env.frotz_lib.getRAMSize)
        if expected != actual:
            logger.warning(
                "Save %s expects %d bytes of RAM, %s has %d", name, expected, self._game, actual
            )
            return (
                (
                    f"That save was written by a different build of {snapshot.game} "
                    "and will not load on this one."
                ),
                False,
            )

        try:
            await asyncio.to_thread(env.set_state, snapshot.state)
        except Exception as exc:
            logger.exception("Could not restore %s", name)
            return f"The machine chokes on that save: {exc}", False

        _a, interval, counter = snapshot.state[7]
        if interval == 0:
            env.frotz_lib.setRng(_fresh_rng_a(), interval, counter)

        # Taken from the restored interpreter rather than the file: the header
        # is what the screen said, the env is what is actually true now.
        self._observation = snapshot.observation
        self._info = {"score": env.get_score(), "moves": env.get_moves()}
        self._done = env.game_over() or env.victory()
        self._location = _player_location(env)
        self._run_map = snapshot.personal_map or {}
        if merge_maps(self._known_map, self._run_map):
            await self._persist_known_map()
        logger.info("Restored %s from %s", self._game, name)

        # The autosave has to follow the machine, or a resume after this would
        # quietly rewind to before the load.
        await self._autosave()
        return f"Restored {snapshot.game} from {name}.\n\n{self.screen()}", True

    async def _remember_reload_reason(
        self,
        game: str,
        signature: StorySignature,
        location: str,
        reason: str,
    ) -> None:
        """Append a significant reason without risking the completed restore."""
        if self._game_memories is None:
            return
        try:
            await asyncio.to_thread(
                self._game_memories.append_reload_reason,
                game,
                signature,
                location,
                reason,
            )
        except GameMemoryError:
            logger.exception("Could not remember reload reason for %s", game)

    async def _recall_reload_reasons(self) -> str | None:
        """Render this game's significant prior reloads as a private reminder."""
        if self._game_memories is None or self._game is None or self._story_signature is None:
            return None
        try:
            reasons = await asyncio.to_thread(
                self._game_memories.recall_reload_reasons,
                self._game,
                self._story_signature,
            )
        except GameMemoryError:
            logger.exception("Could not recall reload reasons for %s", self._game)
            return None
        if not reasons:
            return None
        lines = [
            "You've played this game before and learned some hard lessons. In particular:",
            "",
        ]
        lines.extend(f"- In {item['location']}: {item['reason']}" for item in reasons)
        return "\n".join(lines)

    async def resume(self) -> str | None:
        """Come back to where the autosave left off, if there is anywhere to come back to.

        Returns the screen the machine came up on, or ``None`` if this session
        never wrote an autosave or its autosave will not load. Neither is an
        error: most sessions are new, and the DOS prompt is a perfectly good
        place for a machine to be.
        """
        if self._saves is None:
            return None
        try:
            await asyncio.to_thread(self._saves.info, AUTOSAVE)
        except SaveError:
            return None  # no autosave, which is the ordinary case

        screen = await self.load(AUTOSAVE)
        if self._game is None:
            logger.warning("Nothing came back from the autosave: %s", screen)
            return None
        return screen

    def list_saves(self) -> list[SaveInfo]:
        """List the numbered saves this session has written. Never the autosave."""
        return self._saves.list() if self._saves is not None else []

    def save_info(self, name: str) -> SaveInfo:
        """Read one save's metadata for a detailed listing."""
        if self._saves is None:
            raise SaveError("this machine has no disk to read saves from")
        return self._saves.info(name)

    def _write(self, name: str, comment: str | None = None) -> str:
        """Snapshot the running game under *name*. Runs off the event loop."""
        if self._env is None or self._saves is None or self._game is None:
            raise SaveError("nothing is running")
        return self._saves.write(
            name,
            Snapshot(
                game=self._game,
                state=self._env.get_state(),
                observation=self._observation,
                score=self._info.get("score"),
                moves=self._info.get("moves"),
                done=self._done,
                location=self._location[1] if self._location is not None else None,
                comment=comment,
                personal_map=self._run_map,
                story_signature=self._story_signature,
            ),
        )

    def _load_known_map(self) -> KnownMap:
        """Read the durable map for the running story, or start without one."""
        self._known_map_writable = True
        if self._game_memories is None or self._game is None or self._story_signature is None:
            return {}
        try:
            return self._game_memories.read_map(self._game, self._story_signature)
        except GameMemoryError as exc:
            # Do not overwrite an unreadable file with a fresh empty map after
            # the next move. Keep playing, but leave that artifact untouched.
            self._known_map_writable = False
            logger.warning("Could not load known map for %s: %s", self._game, exc)
            return {}

    async def _persist_known_map(self) -> None:
        """Keep shared route knowledge durable without risking the game move."""
        if (
            not self._known_map_writable
            or self._game_memories is None
            or self._game is None
            or self._story_signature is None
        ):
            return
        try:
            await asyncio.to_thread(
                self._game_memories.write_map,
                self._game,
                self._story_signature,
                self._known_map,
            )
        except GameMemoryError:
            logger.exception("Could not save known map for %s", self._game)

    async def _autosave(self) -> None:
        """Keep the restore point level with the screen.

        A failed autosave is logged and swallowed: losing the restore point is
        a great deal better than losing the move that was just made.
        """
        if self._saves is None or self._env is None:
            return
        try:
            await asyncio.to_thread(self._write, AUTOSAVE)
        except Exception:
            logger.exception("Could not autosave %s", self._game)

    def reboot(self) -> str:
        """Drop whatever is running and come back up at the prompt."""
        was = self._game
        if self._env is not None:
            try:
                self._env.close()
            except Exception:
                logger.exception("Error closing %s", was)
        self._env, self._game, self._story_signature = None, None, None
        self._observation, self._info, self._done = "", {}, False
        self._location, self._run_map, self._known_map = None, {}, {}
        self._known_map_writable = True
        self._hint_availability_pending = False
        if was:
            logger.info("Rebooted, quitting %s", was)
        return NO_GAME


def _join_remarks(*remarks: str | None) -> str | None:
    """Combine private reminders without adding empty personal-remarks blocks."""
    kept = [remark.strip() for remark in remarks if remark and remark.strip()]
    return "\n\n".join(kept) or None


def _instead_of_typing(text: str) -> str | None:
    """Answer a save, resume, or exit command here rather than typing it at the game."""
    line = " ".join(text.casefold().split()).rstrip(".!?")
    verb, _, _rest = line.partition(" ")
    if verb in GAME_SAVE_VERBS or verb in GAME_RESTORE_VERBS or verb in GAME_STOP_VERBS:
        return USE_THE_TOOLS
    return None


def _player_location(env: FrotzEnv) -> Location | None:
    """Return Jericho's stable object number and display name for the room."""
    try:
        location = env.get_player_location()
    except (AttributeError, RuntimeError):
        return None
    if location is None:
        return None
    return location.num, location.name


def _story_signature(path: Path) -> StorySignature | None:
    """Read the conventional identity fields from a Z-machine story header."""
    try:
        with path.open("rb") as handle:
            header = handle.read(_HEADER_NEEDED)
    except OSError:
        logger.exception("Could not read the story header from %s", path)
        return None
    if len(header) < _HEADER_NEEDED:
        logger.warning("Story file %s is too short to carry a signature", path)
        return None
    return StorySignature(
        release=int.from_bytes(header[_RELEASE], "big"),
        serial=header[_SERIAL].decode("latin-1"),
        checksum=int.from_bytes(header[_CHECKSUM], "big"),
    )


def _classify_visit(location: Location, run_map: RunMap, known_map: KnownMap) -> VisitKind:
    """Classify a destination before the movement into it is recorded."""
    if _map_contains(run_map, location):
        return VisitKind.REVISIT_THIS_RUN
    if _map_contains(known_map, location):
        return VisitKind.FIRST_THIS_RUN
    return VisitKind.FIRST_EVER


def _map_contains(route_map: RunMap | KnownMap, location: Location) -> bool:
    """Whether a room appears as either end of a recorded route."""
    return location in route_map or any(location in routes for routes in route_map.values())


def _describe_routes(
    location: Location,
    run_map: RunMap,
    known_map: KnownMap,
    visit: VisitKind,
) -> str:
    """Describe room familiarity and routes known before entering it."""
    lines = ["You've been keeping a personal map using paper and pencil.", ""]
    routes = known_map.get(location, {})

    if visit is VisitKind.FIRST_EVER:
        lines.extend(
            (
                "Neither of you has encountered this room in any recorded run of this game.",
                "You add it to the map.",
            )
        )
        return "\n".join(lines)

    if visit is VisitKind.FIRST_THIS_RUN:
        if not routes:
            lines.append(
                "You recognize this room from an earlier run of this game, but your map has "
                "no routes recorded from here."
            )
            return "\n".join(lines)
        lines.extend(
            (
                (
                    "This is the first time you've entered this room in the current run, but you "
                    "recognize it from an earlier run of this game. Your map records these routes "
                    "from here:"
                ),
                "",
            )
        )
    elif not routes:
        lines.append(
            "You've visited this room before in the current run, but your map has no routes "
            "recorded from here."
        )
        return "\n".join(lines)
    else:
        lines.extend(
            (
                (
                    "You've visited this room before in the current run. Your map records these "
                    "routes from here:"
                ),
                "",
            )
        )

    run_routes = run_map.get(location, {})
    for destination, command in routes.items():
        if run_routes.get(destination) == command:
            lines.append(f"- {command!r} led to {destination[1]!r} (current run)")
        else:
            lines.append(f"- {command!r} previously led to {destination[1]!r} (earlier run only)")
    return "\n".join(lines)
