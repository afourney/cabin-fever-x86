"""The game session that sits behind a websocket connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID, uuid4

from openai import AsyncOpenAI, OpenAIError
from openai.types.responses import FunctionToolParam, ResponseFunctionToolCall

from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.hints import has_hints
from cabin_fever_x86_core.messages import AssistantMessage, UserMessage
from cabin_fever_x86_core.server._ai_client import create_client
from cabin_fever_x86_core.server._compaction import (
    Excuse,
    draw_excuse,
    load_excuses,
    notes_request,
    rebuilt,
    rewrite,
    rotate,
)
from cabin_fever_x86_core.server._game_memories import GAME_MEMORIES_DIR, GameMemoryStore
from cabin_fever_x86_core.server._machine import Machine
from cabin_fever_x86_core.server._saves import SAVES_DIR, SaveStore
from cabin_fever_x86_core.server._system_prompt import SYSTEM_PROMPT
from cabin_fever_x86_core.server._tools import (
    AfkTool,
    ListGamesTool,
    ListSavedGamesTool,
    LoadGameTool,
    NewGameTool,
    NoopTool,
    ReadScreenTool,
    RebootTool,
    RequestHintTool,
    SaveGameTool,
    Tool,
    ToolOutput,
    TransmitTool,
    TypeTool,
)
from cabin_fever_x86_core.sessions import MESSAGES_FILE, SERVER_COMPONENT, session_dir

logger = logging.getLogger(__name__)

SendCallback = Callable[[AssistantMessage], Awaitable[None]]

# What a tool call gets in place of the result it never lived to see.
INTERRUPTED_TOOL_OUTPUT = "Tool call was interrupted, and did not complete."

# How many ordinary model rounds one player transmission may take before we
# force one final transmission, in case the model never settles.
MAX_MODEL_ROUNDS = 8

# Long enough that nothing the operator says lands in the gap while the night is
# being written down, and cleared again the moment it has been.
COMPACTION_AFK = 300.0

# Returned to public compact() callers when the game closes before their
# request reaches a successful end.
COMPACTION_INTERRUPTED = "Game closed before compaction completed"

# What interrupts the game, and where each kind is written down. How far apart
# they arrive is config.
CABIN_EVENT = "cabin_event"
STAGE_DIRECTION = "stage_direction"


def _log_token_usage(response: Any) -> None:
    """Log the token breakdown returned with a model response."""
    usage = response.usage
    if usage is None:
        return

    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    logger.info(
        "Input Tokens: %d (%d cached); Output Tokens: %d (%d reasoning)",
        getattr(usage, "input_tokens", 0),
        getattr(input_details, "cached_tokens", 0),
        getattr(usage, "output_tokens", 0),
        getattr(output_details, "reasoning_tokens", 0),
    )


INTERRUPTION_FILES: dict[str, Path] = {
    CABIN_EVENT: Path(__file__).with_name("cabin_events.txt"),
    STAGE_DIRECTION: Path(__file__).with_name("stage_direction.txt"),
}

# Every session opens with this one, so the cabin speaks first. It is not part
# of the deck: it fires once, at the start, and is never drawn again.
OPENING_DIRECTION = (
    "Key up the radio and reach out to the operator. Keep it short -- they have not "
    'said anything yet — something simple. Like "Hello? ... Hello? Is anyone monitoring '
    'this channel tonight?" This is just an example -- it should sounds like you... '
    "something you would say. Anyhow, the channel is quiet and you are the one opening it. "
    "Do not immediately ask to play the text-based game. Instead, allow the conversation "
    "to start naturally."
)

# And this one when the night is being picked back up instead. The conversation
# has been read back off the disk, so the companion already knows how the
# evening went; what it must not do is start over or make an occasion of it.
REOPENING_DIRECTION = (
    "The operator is back on the channel. You do not know how long they were gone and "
    "neither of you needs to make a thing of it. Say something short to let them know "
    "you are still here — in your own voice, the way you would to someone who just sat "
    "back down. Do not recap the night, do not list what you have been up to, do not "
    "greet them as though you have never spoken, and do not ask where they went. If a "
    "game is running you may say where it stands in a sentence, but read the screen "
    "first rather than trusting your memory of it."
)


@dataclass
class Interruption:
    """Something handed to the companion that the player neither sent nor sees.

    A ``cabin_event`` is something that happened in the cabin and may be
    ignored; a ``stage_direction`` is a nudge that must be acted on. The kind
    doubles as the tag it arrives in.
    """

    kind: str
    text: str
    id: UUID = field(default_factory=uuid4)

    @property
    def content(self) -> str:
        return f"<{self.kind}>{self.text}</{self.kind}>"


@dataclass
class CompactionRequest:
    """A request for the worker to compact after every earlier inbox item."""

    completed: asyncio.Future[None]
    id: UUID = field(default_factory=uuid4)


def _read_lines(path: Path) -> list[str]:
    """Read one entry per line, skipping blanks and ``#`` comments."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("Nothing to read from %s: %s", path, exc)
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def resolve_interrupted_calls(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Answer every tool call that never got an answer, and say that is what happened.

    A call is recorded the moment the model asks for it, and its result only once
    the tool has run. A server that stops in between leaves a call with nothing
    after it — and the API will not accept a conversation in that state, so the
    session could never be resumed at all until the gap is filled.

    The answers go on the end, which is where the calls missing them are: a call
    is only ever left open by a stop between asking for it and recording what it
    returned, and nothing is written in between. Repairing on the way in keeps it
    that way, since the gap is closed before the conversation grows again.
    """
    answered = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    unanswered = [
        item["call_id"]
        for item in items
        if item.get("type") == "function_call" and item.get("call_id") not in answered
    ]
    return [
        *items,
        *(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": INTERRUPTED_TOOL_OUTPUT,
            }
            for call_id in unanswered
        ),
    ]


def load_journal(path: Path) -> list[dict[str, Any]]:
    """Read a conversation back off the disk, in the shape it goes back to the API in.

    A line that will not parse is dropped with a warning rather than taking the
    session down: the likeliest cause is a half-written last line from a server
    that was killed mid-sentence, and the rest of the night is still good.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []  # a new session, which is the ordinary case
    except OSError as exc:
        logger.warning("Could not read the conversation back from %s: %s", path, exc)
        return []

    items: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Dropping %s line %d: %s", path.name, number, exc)
            continue
        if isinstance(item, dict):
            items.append(item)
        else:
            logger.warning("Dropping %s line %d: not an object", path.name, number)
    return items


def load_interruptions(files: dict[str, Path] = INTERRUPTION_FILES) -> list[Interruption]:
    """Load every cabin event and stage direction into one deck."""
    return [
        Interruption(kind=kind, text=text)
        for kind, path in files.items()
        for text in _read_lines(path)
    ]


class Game:
    """A single game session.

    A game belongs to a session: pass *session_id* to pick an existing one back
    up, or leave it out to mint a new one. The game creates the AI client
    described by *config* and keeps its data under
    ``data/sessions/<session_id>/server/``. Callers should hand the id to the
    client so both ends' logs can be correlated.

    The conversation is carried in ``_messages`` and mirrored to
    ``messages.jsonl`` as it grows. Requests run with zero data retention, so
    nothing is held server-side between rounds: every round resends the whole
    context, reasoning included, as encrypted items.

    Incoming transmissions are handed to :meth:`receive`, which queues them and
    returns immediately. A background task works through the queue and pushes
    replies out through the ``send`` callback, so the game can talk whenever it
    has something to say rather than only in response to the player.

    Use it as an async context manager; the client and worker start on entry
    and are shut down on exit::

        async with Game(config, send) as game:
            await game.receive(message)
    """

    def __init__(
        self,
        config: ServerConfig,
        send: SendCallback,
        session_id: UUID | None = None,
    ) -> None:
        self._config = config
        self._send = send
        self._session_id = session_id or uuid4()
        self._client: AsyncOpenAI | None = None
        self._model = ""
        self._data_dir: Path | None = None
        self._messages: list[dict[str, Any]] = []
        self._journal: Path | None = None
        self._deck: list[Interruption] = []
        self._inbox: asyncio.Queue[UserMessage | Interruption | CompactionRequest] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._cabin: asyncio.Task[None] | None = None
        self._afk_until = 0.0
        self._last_player_message_at: float | None = None
        self._resumed = False
        self._excuses: list[Excuse] = []
        # Saves belong to the session, and the folder is left to the store to
        # create on its first write rather than made here for a game that may
        # never be played.
        server_dir = session_dir(self._session_id, SERVER_COMPONENT, create=False)
        saves = SaveStore(server_dir / SAVES_DIR)
        game_memories = GameMemoryStore(server_dir / GAME_MEMORIES_DIR)
        self._machine = Machine(saves=saves, game_memories=game_memories)
        self._tools: dict[str, Tool] = {
            tool.name: tool
            for tool in (
                TransmitTool(self._transmit),
                AfkTool(self._transmit, self._set_afk),
                NoopTool(),
                ListGamesTool(self._machine),
                ReadScreenTool(self._machine),
                TypeTool(self._machine),
                NewGameTool(self._machine),
                SaveGameTool(self._machine),
                LoadGameTool(self._machine),
                ListSavedGamesTool(self._machine),
                RebootTool(self._machine),
            )
        }

    @property
    def session_id(self) -> UUID:
        """Identifies this session on both ends of the connection."""
        return self._session_id

    async def __aenter__(self) -> Self:
        self._data_dir = session_dir(self._session_id, SERVER_COMPONENT)
        self._journal = self._data_dir / MESSAGES_FILE

        # Everything said last time, back in the shape it goes to the API in.
        # Read before the worker starts, so nothing can be answered out of a
        # conversation that is only half here.
        loaded = await asyncio.to_thread(load_journal, self._journal)

        # And before anything is added to it: a call left hanging by a server
        # that stopped mid-turn has to be answered where it happened, not after
        # whatever gets said next.
        self._messages = resolve_interrupted_calls(loaded)
        if len(self._messages) != len(loaded):
            logger.warning(
                "%d tool call(s) in %s never finished; answering them so the night can go on",
                len(self._messages) - len(loaded),
                self._journal.name,
            )
            # Written back, so the gap is closed on the disk too rather than
            # being papered over again on every resume from here on.
            await asyncio.to_thread(rewrite, self._journal, self._messages)

        self._resumed = bool(self._messages)
        if self._resumed:
            logger.info("Picked the conversation back up: %d item(s)", len(self._messages))
        self._client, self._model = create_client(self._config.ai_client, str(self._session_id))
        self._tools[RequestHintTool.name] = RequestHintTool(
            self._client,
            self._model,
            self._machine,
        )
        logger.info(
            "Session %s: provider %r, model %r, data in %s",
            self._session_id,
            self._config.ai_client.provider,
            self._model,
            self._data_dir,
        )
        # Before the worker, so nothing can be answered while the machine is
        # still at the prompt. A resumed session replays the conversation from
        # the journal, so the companion comes back remembering the game it was
        # playing; without this the machine would come up empty and make a liar
        # of it. A new session has no autosave and simply starts at the prompt.
        resumed = await self._machine.resume()
        if resumed is not None:
            logger.info("Resumed %s where the autosave left it", self._machine.game)

        self._excuses = load_excuses()
        logger.info(
            "Compacting past %d tokens, with %d excuse(s) to do it behind",
            self._config.compaction_threshold,
            len(self._excuses),
        )

        self._worker = asyncio.create_task(self._run())

        deck = load_interruptions()
        if deck:
            self._cabin = asyncio.create_task(self._run_interruptions(deck))
            counts = Counter(item.kind for item in deck)
            logger.info("Loaded %s", ", ".join(f"{n} {kind}s" for kind, n in counts.items()))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for name in ("_cabin", "_worker"):
            task = getattr(self, name)
            setattr(self, name, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        while True:
            try:
                pending = self._inbox.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(pending, CompactionRequest) and not pending.completed.done():
                pending.completed.set_exception(RuntimeError(COMPACTION_INTERRUPTED))
            self._inbox.task_done()

        self._machine.reboot()

        client = self._client
        self._client = None
        if client is not None:
            await client.close()

    async def receive(self, message: UserMessage) -> None:
        """Accept a transmission from the player, unless nobody is at the radio."""
        if self._worker is None:
            raise RuntimeError("Game.receive() called outside of the game context")
        self._last_player_message_at = time.monotonic()
        if self._afk_seconds_left() > 0:
            logger.info("Away from the radio; missed %s", message.id)
            return
        await self._inbox.put(message)

    async def cabin_event(self, text: str) -> None:
        """Deliver something that just happened in the cabin.

        Handled like a transmission from the player, except that the player
        neither sent it nor knows about it, and the companion may let it pass.
        """
        await self._interrupt(Interruption(kind=CABIN_EVENT, text=text))

    async def stage_direction(self, text: str) -> None:
        """Nudge the companion to do something. Unlike a cabin event, not optional."""
        await self._interrupt(Interruption(kind=STAGE_DIRECTION, text=text))

    def opening_direction(self) -> str:
        """Choose what to speak first: a fresh hail, or picking the night back up."""
        return REOPENING_DIRECTION if self._resumed else OPENING_DIRECTION

    async def open_channel(self) -> None:
        """Have the companion speak first, before the player has said anything.

        Call this once the client knows its session id, so the opening
        transmission does not race the handshake.
        """
        await self.stage_direction(self.opening_direction())

    async def compact(self) -> None:
        """Queue compaction behind any in-flight turns and wait for it to finish."""
        if self._worker is None:
            raise RuntimeError("Game.compact() called outside of the game context")
        completed = asyncio.get_running_loop().create_future()
        await self._inbox.put(CompactionRequest(completed))
        await completed

    async def _interrupt(self, interruption: Interruption) -> None:
        if self._worker is None:
            raise RuntimeError("Game interrupted outside of the game context")
        await self._inbox.put(interruption)

    def _draw(self) -> Interruption:
        """Take the next interruption from the deck, reshuffling once it runs out."""
        if not self._deck:
            self._deck = load_interruptions()
            random.shuffle(self._deck)
            logger.info("Reshuffled %d interruption(s)", len(self._deck))
        return self._deck.pop()

    async def _run_interruptions(self, deck: list[Interruption]) -> None:
        """Interrupt the conversation now and then, forever, without repeating."""
        self._deck = list(deck)
        random.shuffle(self._deck)
        delays = self._config.cabin_events

        while True:
            await asyncio.sleep(random.uniform(delays.min_delay, delays.max_delay))
            await self._emit_scheduled_interruption()

    def _player_is_active(self) -> bool:
        """Whether the player has transmitted recently enough for a cabin interruption."""
        last = self._last_player_message_at
        if last is None:
            return False
        return time.monotonic() - last <= self._config.cabin_events.inactivity_timeout

    async def _emit_scheduled_interruption(self) -> None:
        """Draw and queue one interruption, provided the player is still active."""
        if not self._player_is_active():
            return
        item = self._draw()
        if not self._player_is_active():
            self._deck.append(item)
            return
        logger.info("%s: %s", item.kind, item.text)
        if item.kind == STAGE_DIRECTION:
            await self.stage_direction(item.text)
        else:
            await self.cabin_event(item.text)

    async def _transmit(self, content: str) -> None:
        """Speak one line to the player."""
        await self._send(AssistantMessage(content=content))

    def _set_afk(self, seconds: float) -> None:
        self._afk_until = time.monotonic() + seconds

    def _afk_seconds_left(self) -> float:
        return max(0.0, self._afk_until - time.monotonic())

    async def _run(self) -> None:
        while True:
            message = await self._inbox.get()
            try:
                if isinstance(message, CompactionRequest):
                    tools = [tool.definition for tool in self._tools.values()]
                    await self._compact([], 0, tools)
                    if not message.completed.done():
                        message.completed.set_result(None)
                else:
                    await self._handle(message)
            except Exception as exc:
                logger.exception("Error while handling %s", message.id)
                if isinstance(message, CompactionRequest) and not message.completed.done():
                    message.completed.set_exception(exc)
            finally:
                if isinstance(message, CompactionRequest) and not message.completed.done():
                    message.completed.set_exception(RuntimeError(COMPACTION_INTERRUPTED))
                self._inbox.task_done()

    def _append(self, item: dict[str, Any]) -> None:
        """Add one item to the running context and to ``messages.jsonl``.

        The journal is the record a future resume will read back, so it is
        written in the same shape the API expects on the way back in.
        """
        self._messages.append(item)
        if self._journal is None:
            return
        with self._journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item) + "\n")

    async def _run_tool(
        self,
        call: ResponseFunctionToolCall,
        cabin_turn: bool,
        *,
        force_transmit: bool = False,
    ) -> ToolOutput:
        """Carry out one tool call, turning any failure into a result the model can read.

        Nothing that goes wrong here ends the turn: the companion is left able
        to try again, or to say something instead.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("Model called unknown tool %r", call.name)
            return ToolOutput(f"There is no tool called {call.name!r}.")
        if tool.cabin_event_only and not cabin_turn:
            logger.warning("Model called %r outside a cabin event", call.name)
            return ToolOutput(f"{call.name!r} is only available in response to a cabin event.")

        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return ToolOutput(f"Those arguments were not valid JSON: {exc}")
        if not isinstance(args, dict):
            return ToolOutput("Arguments must be a JSON object.")

        try:
            if force_transmit and isinstance(tool, TransmitTool):
                # The final model round has no retry left. Let its transmission
                # through even when it exceeds the usual radio limit.
                return await tool.execute(args, force=True)
            return await tool.execute(args)
        except Exception as exc:
            logger.exception("Tool %r failed", call.name)
            return ToolOutput(f"The tool failed: {exc}")

    async def _compact(
        self,
        tail: list[dict[str, Any]],
        used: int,
        tools: list[FunctionToolParam],
    ) -> None:
        """Step away, write the night down, and carry on from the notes.

        *tail* is the reply that tripped the threshold, already in the journal
        and kept on the far side of the notes. Nothing here is fatal: if the
        notes do not come back the conversation goes on at full length, which
        risks the next request but is better than dropping the session on the
        floor mid-sentence.
        """
        if self._client is None:
            return

        excuse = draw_excuse(self._excuses)
        logger.info(
            "Compacting at %d tokens, %d item(s) in the conversation", used, len(self._messages)
        )
        if excuse is not None:
            await self._transmit(excuse.away)

        # Away from the radio while the notes are written, so nothing the
        # operator says lands in a gap they were told about.
        self._set_afk(COMPACTION_AFK)
        try:
            history = self._messages[: len(self._messages) - len(tail)]
            response = await self._client.responses.create(
                model=self._model,
                prompt_cache_key=str(self._session_id),
                instructions=SYSTEM_PROMPT,
                input=notes_request(history),
                # The tools stay declared because the conversation being summarised
                # is full of calls to them, and refused because notes are wanted,
                # not another move.
                tools=tools,
                tool_choice="none",
                reasoning={"effort": "medium"},
                store=False,
            )
            _log_token_usage(response)
            summary = response.output_text.strip()
            if not summary:
                raise OpenAIError("the notes came back empty")
        except OpenAIError:
            logger.exception("Could not write the night down; carrying on at full length")
            if excuse is not None:
                await self._transmit(excuse.back)
            return
        finally:
            self._afk_until = 0.0

        self._messages = rebuilt(summary, excuse, tail)
        if self._journal is not None:
            aside = await asyncio.to_thread(rotate, self._journal)
            await asyncio.to_thread(rewrite, self._journal, self._messages)
            logger.info(
                "Wrote the night down in %d characters, old journal at %s", len(summary), aside
            )

        if excuse is not None:
            await self._transmit(excuse.back)

    async def _handle(self, message: UserMessage | Interruption) -> None:
        if self._client is None:
            raise RuntimeError("Game is not running")

        # A cabin event may be let pass; a stage direction may not, so the
        # tools for shrugging one off are kept out of reach on every other turn.
        cabin_turn = isinstance(message, Interruption) and message.kind == CABIN_EVENT
        self._append({"role": "user", "content": message.content})

        # Keep every definition, and its order, stable for prompt caching. What
        # the model may actually call on this turn is narrowed with allowed_tools.
        tools = [tool.definition for tool in self._tools.values()]

        compacted = False
        for model_round in range(MAX_MODEL_ROUNDS + 1):
            final_round = model_round == MAX_MODEL_ROUNDS
            try:
                response = await self._client.responses.create(
                    model=self._model,
                    prompt_cache_key=str(self._session_id),
                    instructions=SYSTEM_PROMPT,
                    input=self._messages,
                    tools=tools,
                    tool_choice=(
                        {"type": "function", "name": TransmitTool.name}
                        if final_round
                        else self._allowed_tool_choice(cabin_turn)
                    ),
                    parallel_tool_calls=False,
                    reasoning={"effort": "medium"},
                    # Zero data retention: nothing is kept server-side between
                    # turns, so the reasoning has to travel with us, encrypted.
                    store=False,
                    include=["reasoning.encrypted_content"],
                )
                _log_token_usage(response)
            except OpenAIError as exc:
                logger.exception("Response failed for %s", message.id)
                await self._transmit(f"[model error: {exc}]")
                return

            calls: list[ResponseFunctionToolCall] = []
            tail: list[dict[str, Any]] = []
            for item in response.output:
                stored = item.model_dump(exclude_none=True)
                tail.append(stored)
                self._append(stored)
                if isinstance(item, ResponseFunctionToolCall):
                    calls.append(item)

            # Checked here rather than before the tool calls run: the reply is
            # already in hand, and *tail* carries it across so a call it has
            # decided on still gets answered on the other side. Once per
            # transmission, however long the night gets — a summary that is
            # itself over the threshold is a problem to log, not to loop on.
            used = response.usage.total_tokens if response.usage else 0
            if used >= self._config.compaction_threshold and not compacted:
                compacted = True
                await self._compact(tail, used, tools)

            if not calls:
                # Nothing called: whatever it said in plain text is the transmission.
                text = response.output_text.strip()
                await self._transmit(text)
                return

            done = False
            # Parallel calls are disabled above, so there should currently be
            # at most one. Keep handling the full response shape here in case
            # we choose to allow batches again later.
            for call in calls:
                result = await self._run_tool(
                    call,
                    cabin_turn,
                    force_transmit=final_round and call.name == TransmitTool.name,
                )
                self._append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": result.for_model(),
                    }
                )
                done = done or result.end_turn

            if done:
                return

        logger.warning(
            "Forced transmission for %s did not end the turn after %d model rounds",
            message.id,
            MAX_MODEL_ROUNDS,
        )

    def _allowed_tool_choice(self, cabin_turn: bool) -> dict[str, Any]:
        """Restrict calls without changing the cacheable tool definitions."""
        allowed: list[dict[str, str]] = []
        for tool in self._tools.values():
            if tool.cabin_event_only and not cabin_turn:
                continue
            if isinstance(tool, RequestHintTool) and not has_hints(self._machine.game):
                continue
            allowed.append({"type": "function", "name": tool.name})
        return {"type": "allowed_tools", "mode": "auto", "tools": allowed}
