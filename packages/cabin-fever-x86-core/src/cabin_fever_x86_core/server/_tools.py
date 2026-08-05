"""The tools the companion can call.

Each tool carries its own definition in the shape the responses API expects,
and knows how to carry itself out. Tools reach the outside world through
callbacks handed to them at construction, so they can be exercised without a
game or a websocket behind them.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from openai.types.responses import FunctionToolParam

from cabin_fever_x86_core.server._saves import SaveError

if TYPE_CHECKING:  # Machine names these tools in its own messages, so the
    # runtime dependency only runs one way: Machine imports us, not the reverse.
    from cabin_fever_x86_core.server._machine import Machine

logger = logging.getLogger(__name__)

# Speaks one line to the player.
TransmitCallback = Callable[[str], Awaitable[None]]
# Marks the companion away for the given number of seconds.
AfkCallback = Callable[[float], None]

DEFAULT_AFK_DELAY = 20.0
MIN_AFK_DELAY = 10.0
MAX_AFK_DELAY = 60.0

# How much the set will pass in one go. The companion is never told this
# number — it just finds out, the way anyone on a bad channel would.
MAX_TRANSMISSION = 350
PERSONAL_REMARKS = "personal_remarks"

# What it sounds like when a transmission was too long to make it out. Varied,
# so a run of them does not read like the same error twice.
CUT_OUT = (
    (
        "Your transmission cut out. Try again, but maybe a *little* shorter in "
        "case there's more radio interference. But, don't get all terse about it — "
        "the operator is not a fan of clipped sentences."
    ),
    (
        "Squelch broke halfway through and none of that got out. Say it again, "
        "and keep it a *tad* tighter — the band is not being kind tonight. Not "
        "clipped, though; they called in for conversation, not a weather bulletin."
    ),
    (
        "The repeater dropped you mid-sentence. They heard nothing. Go again, a "
        "*little* shorter this time — trim it, don't gut it. Still sound like you."
    ),
    (
        "Static swallowed the back half of that one. Nothing reached them. "
        "Try it again with just a few fewer words, but keep it warm — nobody "
        "wants to be answered in fragments."
    ),
    (
        "Carrier folded before you finished — that one did not make it out. "
        "Send it again, but a *little* tighter. Losing a sentence is fine; "
        "losing the tone is not."
    ),
)


@dataclass
class ToolOutput:
    """What one call produced, and whether it finishes the companion's turn.

    A tool that has spoken to the player has said its piece, exactly like a
    plain reply with no tool calls; going back to the model afterwards only
    invites it to say the same thing twice.
    """

    content: str
    end_turn: bool = False
    remarks: str | None = None

    def for_model(self) -> str:
        """Render content with an optional private reminder for the companion."""
        if not self.remarks:
            return self.content
        remarks = html.escape(self.remarks, quote=False).strip()
        return f"{self.content}\n\n<{PERSONAL_REMARKS}>\n{remarks}\n</{PERSONAL_REMARKS}>"


class Tool(ABC):
    """A function the companion can call."""

    name: ClassVar[str]
    description: ClassVar[str]
    parameters: ClassVar[dict[str, Any]]

    #: Whether this is only offered on a cabin-event turn. Tools that make no
    #: sense as an answer to the player are kept out of their reach entirely,
    #: rather than being offered and then refused.
    cabin_event_only: ClassVar[bool] = False

    @property
    def definition(self) -> FunctionToolParam:
        """The tool as the responses API wants to see it."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": False,
        }

    @abstractmethod
    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        """Carry out one call, returning what the model should see as its result."""


class TransmitTool(Tool):
    """Says something to the player over the radio."""

    name = "transmit"
    description = (
        "Speak to the operator over the radio. This is how you say anything "
        "you want them to hear, and it ends your turn: say it all in one call."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "What you say, in your own voice, as one transmission.",
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    }

    def __init__(self, transmit: TransmitCallback) -> None:
        self._transmit = transmit
        self._cut_out_last = False

    async def execute(self, args: dict[str, Any], *, force: bool = False) -> ToolOutput:
        message = str(args.get("message") or "").strip()
        if not message:
            # Silence is still a transmission. The client decides how to render
            # it: an empty line, radio static, or something else appropriate to
            # that client.
            await self._transmit("")
            return ToolOutput(
                "Kerchunk. You keyed up the radio without speaking.",
                end_turn=True,
            )

        # Never twice running: a second long one gets through regardless, so a
        # companion that cannot be brief is not left transmitting into a void.
        if len(message) > MAX_TRANSMISSION and not self._cut_out_last and not force:
            # Nothing goes out, and the turn stays open so it can be said again.
            self._cut_out_last = True
            logger.info("Transmission cut out at %d characters", len(message))
            return ToolOutput(
                random.choice(CUT_OUT)
                + "\n We can probably get through about "
                + str(MAX_TRANSMISSION)
                + " characters in one go, so just keep it under that.",
                end_turn=False,
            )

        if len(message) > MAX_TRANSMISSION:
            logger.info("Letting %d characters through after a cut-out", len(message))

        self._cut_out_last = False
        await self._transmit(message)
        return ToolOutput("Transmitted.", end_turn=True)


class AfkTool(Tool):
    """Steps away from the radio for a while, missing anything said meanwhile."""

    name = "afk"
    description = (
        "Step away from the radio for a while — to make coffee, feed the stove, "
        "check the antenna. Say what you are doing as you go and again when you "
        "get back. Anything the operator transmits while you are away is missed "
        "entirely, so do not step away mid-thought or when they are waiting on you."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "leaving_message": {
                "type": "string",
                "description": "What you say before setting the handset down.",
            },
            "returning_message": {
                "type": "string",
                "description": "What you say when you pick it back up.",
            },
            "delay": {
                "type": "number",
                "description": (
                    f"Seconds away, between {MIN_AFK_DELAY:.0f} and {MAX_AFK_DELAY:.0f}. "
                    f"Defaults to {DEFAULT_AFK_DELAY:.0f}."
                ),
                "minimum": MIN_AFK_DELAY,
                "maximum": MAX_AFK_DELAY,
                "default": DEFAULT_AFK_DELAY,
            },
        },
        "required": ["leaving_message", "returning_message"],
        "additionalProperties": False,
    }

    def __init__(self, transmit: TransmitCallback, set_afk: AfkCallback) -> None:
        self._transmit = transmit
        self._set_afk = set_afk

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        delay = _afk_delay(args.get("delay"))
        leaving = str(args.get("leaving_message") or "").strip()
        returning = str(args.get("returning_message") or "").strip()

        if leaving:
            await self._transmit(leaving)

        # Set before sleeping: the game drops the player's transmissions for
        # exactly as long as we are gone.
        self._set_afk(delay)
        logger.info("Away from the radio for %.0fs", delay)
        await asyncio.sleep(delay)

        if returning:
            await self._transmit(returning)
        return ToolOutput(
            f"You were away for {delay:.0f} seconds and are back at the radio. "
            "Both messages have already been sent, so do not repeat them; say "
            "nothing further unless you have something new to add. Anything the "
            "operator transmitted while you were gone did not reach you."
        )


class NoopTool(Tool):
    """Lets a cabin event pass without saying anything about it."""

    name = "noop"
    description = (
        "Let a cabin event pass without acting on it and without saying "
        "anything. Use this when whatever just happened in the cabin is not "
        "worth interrupting the operator over. Only available in response to a "
        "cabin event."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    cabin_event_only = True

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        return ToolOutput("Let it pass.", end_turn=True)


class ListGamesTool(Tool):
    """Reads the disk for what can be played."""

    name = "list_games"
    description = "List the games on the computer's disk."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        games = self._machine.list_games()
        if not games:
            return ToolOutput("There is nothing readable on the disk.")
        return ToolOutput(f"{len(games)} games on the disk: " + ", ".join(games))


class ReadScreenTool(Tool):
    """Looks at the screen again."""

    name = "read_screen"
    description = (
        "Look at the computer screen: which game is running and whatever it "
        "last printed. Nothing is typed and nothing changes. Start here when "
        "you are unsure what is on the machine — do not assume a game is loaded."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        return ToolOutput(self._machine.screen())


class TypeTool(Tool):
    """Types a line at the machine."""

    name = "type"
    description = (
        "Type one line at the computer and read what comes back. With a game "
        "running this is a command to the game ('open mailbox', 'go north'). "
        "At the DOS prompt with nothing running, type the name of a game to "
        "load it."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The line to type — one game command, or a game name to load.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        content, remarks = await self._machine.type_text(str(args.get("text") or ""))
        return ToolOutput(content, remarks=remarks)


class NewGameTool(Tool):
    """Starts a game from the beginning, whatever was running before."""

    name = "new_game"
    description = (
        "Start a game from the beginning. Whatever is running is dropped first "
        "and the machine comes up on the new game's opening screen, so this "
        "works from anywhere — there is no need to reboot first, and no need to "
        "be at the DOS prompt. Progress in whatever was running is lost unless "
        "it was saved. Use this to start something new, and to start the same "
        "game over from the top; use list_games to see what there is."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "rom_name": {
                "type": "string",
                "description": (
                    "Which game to start, as list_games gives it. Forgiving: "
                    "'zork1', 'ZORK1' and 'zork1.z5' all work, as does any "
                    "beginning of a name that only one game answers to."
                ),
            },
        },
        "required": ["rom_name"],
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        name = str(args.get("rom_name") or "").strip()
        if not name:
            return ToolOutput("No game was named, so nothing was started.")
        return ToolOutput(await self._machine.new_game(name))


class SaveGameTool(Tool):
    """Writes the running game to the next numbered slot."""

    name = "save_game"
    description = (
        "Save the running game to disk. The slot is numbered for you and comes "
        "back in the result — there is nothing to name. You may attach a short "
        "comment explaining why this point matters. Worth doing before "
        "anything that might get you killed, and before you stop for the night."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "comment": {
                "type": "string",
                "maxLength": 500,
                "description": "An optional short note about why this save was made.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        comment = args.get("comment")
        return ToolOutput(await self._machine.save(comment=str(comment) if comment else None))


class LoadGameTool(Tool):
    """Puts the machine back to a save."""

    name = "load_game"
    description = (
        "Load a saved game, putting the computer back exactly where that save "
        "was written and starting the right game first if it is not already "
        "running. Anything unsaved since is lost. Call list_saved_games first "
        "if you are not certain what a save holds."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "save_name": {
                "type": "string",
                "description": (
                    "Which save to load, as list_saved_games gives it, such as "
                    "'zork1_0003'. Both halves are needed: each game is numbered "
                    "from 0001 on its own."
                ),
            },
        },
        "required": ["save_name"],
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        name = str(args.get("save_name") or "").strip()
        if not name:
            return ToolOutput("No save was named, so nothing was loaded.")
        return ToolOutput(await self._machine.load(name))


class ListSavedGamesTool(Tool):
    """Reads the saves folder."""

    name = "list_saved_games"
    description = (
        "List the saved games on the disk, with which game each one is, its "
        "score, move count, location, comment preview, and when it was written. "
        "Name one save to see its full metadata and complete comment."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "save_name": {
                "type": "string",
                "description": "Optional save name whose full details should be shown.",
            }
        },
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        name = str(args.get("save_name") or "").strip()
        if name:
            try:
                save = await asyncio.to_thread(self._machine.save_info, name)
            except SaveError as exc:
                return ToolOutput(f"That save cannot be described: {exc}")
            return ToolOutput(save.describe_full())
        saves = await asyncio.to_thread(self._machine.list_saves)
        if not saves:
            return ToolOutput("There are no saved games on the disk yet.")
        listing = "\n".join(save.describe() for save in saves)
        count = "1 saved game" if len(saves) == 1 else f"{len(saves)} saved games"
        return ToolOutput(f"{count} on the disk:\n{listing}")


class RebootTool(Tool):
    """Power-cycles the machine, losing whatever was running."""

    name = "reboot"
    description = (
        "Reboot the computer. Whatever game is running is quit and its "
        "progress is lost, and the machine comes back at the DOS prompt."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, machine: Machine) -> None:
        self._machine = machine

    async def execute(self, args: dict[str, Any]) -> ToolOutput:
        return ToolOutput(self._machine.reboot())


def _afk_delay(value: Any) -> float:
    """Clamp the requested delay into range; models do not always respect a schema."""
    if value is None:
        return DEFAULT_AFK_DELAY
    try:
        delay = float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring unreadable afk delay %r", value)
        return DEFAULT_AFK_DELAY

    clamped = min(max(delay, MIN_AFK_DELAY), MAX_AFK_DELAY)
    if clamped != delay:
        logger.warning("Clamped afk delay %.0fs to %.0fs", delay, clamped)
    return clamped
