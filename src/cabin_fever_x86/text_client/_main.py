"""Entry point for the Cabin Fever x86 text client.

Same session protocol as the other two clients; the difference is that the
words are typed. There is no voice on this one, so the terminal does the work
the radio does elsewhere: who is speaking, whether the channel is busy, and
how long the cabin has been sitting on the reply.

    uv run cf86-text
    uv run cf86-text --resume            # the most recent session
    uv run cf86-text --list-sessions

:mod:`cabin_fever_x86.text_client._console` owns the screen. This module owns
the traffic over it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from uuid import UUID

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from cabin_fever_x86.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    ErrorResult,
    ListSessionsCommand,
    SessionInfo,
    SessionListResult,
    UserMessage,
)
from cabin_fever_x86.session_client import (
    SessionCommandError,
    open_session,
)
from cabin_fever_x86.session_client import (
    list_sessions as fetch_sessions,
)
from cabin_fever_x86.sessions import TEXT_CLIENT_COMPONENT
from cabin_fever_x86.text_client._console import Console, make_console
from cabin_fever_x86.transcripts import Transcript

#: ``--resume`` with nothing after it: whatever was played last.
LATEST = "latest"

COMMANDS: dict[str, str] = {
    "/help": "this",
    "/session": "the session id, and where this side is writing it down",
    "/sessions": "what the server has on file",
    "/clear": "wipe the screen",
    "/quit": "hang up",
}

#: Accepted, but not worth a line of its own in /help — nor a place in the
#: completions, where "/q" would only ever get in "/quit"'s way.
ALIASES = {"/exit": "/quit", "/q": "/quit"}


def _resume_argument(value: str) -> UUID | str:
    """Read ``--resume``: a session id, or ``latest`` for the most recent."""
    if value.lower() == LATEST:
        return LATEST
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a session id: {value}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to Cabin Fever x86 over text.")
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host to connect to. Overrides client.host in the config.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to connect to. Overrides client.port in the config.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Print without colour. NO_COLOR in the environment does the same.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--resume",
        metavar="SESSION_ID",
        nargs="?",
        const=LATEST,
        type=_resume_argument,
        default=None,
        help="Resume a session instead of starting one. Bare, it takes the most recent.",
    )
    group.add_argument(
        "--list-sessions",
        action="store_true",
        help="List the sessions the server holds and exit.",
    )
    return parser.parse_args(argv)


def _session_rows(
    sessions: Sequence[SessionInfo], where: str, current: UUID | None = None
) -> list[str]:
    """Lay out a session list, marking *current* if it happens to be in it.

    Both the standalone ``--list-sessions`` and the in-session ``/sessions``
    print this, so the two cannot drift apart.
    """
    rows = [f"{len(sessions)} session(s) on {where}, most recent first:", ""]
    for info in sessions:
        here = "  <- this one" if info.session_id == current else ""
        rows.append(f"  {info.session_id}  {info.modified.isoformat(timespec='seconds')}{here}")
    return rows


async def list_sessions(host: str, port: int) -> None:
    """Print the sessions the server holds data for."""
    uri = f"ws://{host}:{port}"
    async with connect(uri) as connection:
        found = await fetch_sessions(connection)

    if not found:
        print("No sessions on the server yet.")
        return
    print("\n".join(_session_rows(found, uri)))


class TextClient:
    """One typed session: the channel, the record of it, and the terminal.

    Both directions run at once, so a transmission from the cabin can land
    while a reply to the last one is still being typed.
    """

    def __init__(
        self,
        connection: ClientConnection,
        transcript: Transcript,
        console: Console,
        session_id: UUID,
    ) -> None:
        """Hold *connection* open for *session_id*, drawn on *console*."""
        self._connection = connection
        self._transcript = transcript
        self._console = console
        self._session_id = session_id
        self._hung_up = False

    async def run(self) -> None:
        """Pump the channel until the player or the server hangs up."""
        tasks = [
            asyncio.create_task(coro, name=name)
            for name, coro in (("receive", self._receive()), ("send", self._send()))
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, ConnectionClosed):
                raise error

        if not self._hung_up:  # the far end went first
            self._console.say("note", "The channel went quiet. Nobody is on the other end.")

    # -- inbound --

    async def _receive(self) -> None:
        """Print transmissions from the server as they arrive."""
        async for raw in self._connection:
            try:
                message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
            except ValidationError:
                self._console.say("error", f"unreadable message from the server: {raw!r}")
                continue

            if isinstance(message, ErrorResult):
                self._transcript.log("error", message.request_id, message.message)
                self._console.say("error", message.message)
            elif isinstance(message, SessionListResult):
                self._show_sessions(message)
            elif isinstance(message, AssistantMessage):
                self._transcript.log("assistant", message.id, message.content)
                self._console.say("assistant", message.content)

    def _show_sessions(self, result: SessionListResult) -> None:
        """Answer a ``/sessions`` with what came back."""
        if not result.sessions:
            self._console.say("note", "No sessions on the server yet — this one is the first.")
            return
        self._console.listing(_session_rows(result.sessions, "the server", self._session_id))

    # -- outbound --

    async def _send(self) -> None:
        """Read the player's transmissions and send them on."""
        async for line in self._console.lines():
            content = line.strip()
            if not content:
                continue
            if content.startswith("/"):
                self._console.echo(content)
                if not await self._command(content):
                    self._hung_up = True
                    return
                continue

            self._console.say("user", content)
            message = UserMessage(content=content)
            self._transcript.log("user", message.id, content)
            await self._connection.send(message.model_dump_json())
        self._hung_up = True

    async def _command(self, line: str) -> bool:
        """Run a slash command, returning ``False`` when it means to hang up."""
        name, _, _argument = line.partition(" ")
        name = ALIASES.get(name.lower(), name.lower())

        if name == "/quit":
            return False
        if name == "/help":
            width = max(len(command) for command in COMMANDS)
            self._console.listing(
                [f"{command.ljust(width)}   {what}" for command, what in COMMANDS.items()]
                + ["", "Anything else you type goes out over the air."]
            )
        elif name == "/session":
            self._console.listing(
                [f"session   {self._session_id}", f"record    {self._transcript.path}"]
            )
        elif name == "/sessions":
            # Asked over the open socket rather than a second connection, so
            # the answer comes back through _receive with everything else.
            await self._connection.send(ListSessionsCommand().model_dump_json())
        elif name == "/clear":
            self._console.clear()
        else:
            self._console.say("error", f"no such command: {name}  (try /help)")
        return True


async def run_client(
    host: str,
    port: int,
    resume: UUID | str | None = None,
    color: bool = True,
) -> None:
    """Connect, open a session, and hold the channel until somebody leaves."""
    uri = f"ws://{host}:{port}"

    async with connect(uri) as connection:
        if resume == LATEST:
            found = await fetch_sessions(connection)
            if not found:
                raise SessionCommandError("no sessions on the server to resume")
            resume = found[0].session_id
        if resume is not None and not isinstance(resume, UUID):
            # Starting a fresh game is the one thing somebody asking to resume
            # cannot have meant, so say so rather than quietly doing it.
            raise SessionCommandError(f"not a session id: {resume}")

        session_id = await open_session(connection, resume)
        transcript = Transcript(session_id, TEXT_CLIENT_COMPONENT)
        verb = "resumed" if resume else "started"
        transcript.log("session", None, f"{verb} on {connection.remote_address}")

        console = make_console(list(COMMANDS), color=color)
        async with console:
            # Which session this is, and where it is being written down, is a
            # /session away. The screen opens on the tagline instead.
            console.banner(
                "Feeding grues over VHF.",
                f"/help for commands {console.glyphs.dot} Ctrl-D to hang up.",
            )
            await TextClient(connection, transcript, console, session_id).run()


def main() -> None:
    args = _parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    host = args.host if args.host is not None else config.client.host
    port = args.port if args.port is not None else config.client.port

    try:
        if args.list_sessions:
            asyncio.run(list_sessions(host, port))
            return
        asyncio.run(run_client(host, port, args.resume, color=not args.no_color))
    except SessionCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, WebSocketException) as exc:
        print(f"error: could not connect to ws://{host}:{port}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass
    print("Disconnected.")


if __name__ == "__main__":
    main()
