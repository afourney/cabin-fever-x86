"""Entry point for the Cabin Fever x86 websocket server."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import AsyncExitStack
from functools import partial

from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from cabin_fever_x86.config import (
    DEFAULT_CONFIG_PATH,
    ConfigError,
    ServerConfig,
    load_config,
)
from cabin_fever_x86.messages import (
    CLIENT_MESSAGE_ADAPTER,
    ErrorResult,
    ListSessionsCommand,
    ResumeGameCommand,
    ServerMessage,
    SessionCommand,
    SessionListResult,
    SessionResult,
    UserMessage,
)
from cabin_fever_x86.server._download import DownloadError, ensure_games
from cabin_fever_x86.server._game import Game, SendCallback
from cabin_fever_x86.server._machine import GAMES_DIR
from cabin_fever_x86.sessions import SERVER_COMPONENT, find_sessions, session_exists

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Cabin Fever x86 server.")
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--interface",
        default=None,
        help="Interface to listen on. Overrides server.interface in the config.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on. Overrides server.port in the config.",
    )
    return parser.parse_args(argv)


class CommandRefused(Exception):
    """A command the server will not carry out; reported back as an error."""


async def _run_command(
    command: SessionCommand,
    game: Game | None,
    config: ServerConfig,
    send: SendCallback,
    games: AsyncExitStack,
) -> tuple[Game | None, SessionResult | SessionListResult]:
    """Carry out a session command, returning the game it left in place."""
    if isinstance(command, ListSessionsCommand):
        sessions = find_sessions(SERVER_COMPONENT)
        logger.info("Listed %d session(s)", len(sessions))
        return game, SessionListResult(request_id=command.id, sessions=sessions)

    if game is not None:
        raise CommandRefused(f"a game is already running for session {game.session_id}")

    session_id = command.session_id if isinstance(command, ResumeGameCommand) else None
    if session_id is not None and not session_exists(session_id, SERVER_COMPONENT):
        raise CommandRefused(f"no such session: {session_id}")

    try:
        game = await games.enter_async_context(Game(config, send, session_id))
    except Exception as exc:
        logger.exception("Could not start a game")
        raise CommandRefused(f"could not start: {exc}") from exc

    logger.info("%s session %s", "Resumed" if session_id else "Started", game.session_id)
    return game, SessionResult(request_id=command.id, session_id=game.session_id)


async def handle_connection(connection: ServerConnection, config: ServerConfig) -> None:
    """Serve one client connection: session commands first, then the game."""
    peer = connection.remote_address
    logger.info("Client connected: %s", peer)

    async def send(message: ServerMessage) -> None:
        await connection.send(message.model_dump_json())

    # The client drives session setup; no game exists until it asks for one.
    game: Game | None = None

    try:
        async with AsyncExitStack() as games:
            async for raw in connection:
                try:
                    message = CLIENT_MESSAGE_ADAPTER.validate_json(raw)
                except ValidationError as exc:
                    logger.warning("Discarding malformed message from %s: %s", peer, exc)
                    await send(ErrorResult(message=f"malformed message: {exc}"))
                    continue

                try:
                    if isinstance(message, UserMessage):
                        if game is None:
                            raise CommandRefused(
                                "no game in progress; send new_game or resume_game first"
                            )
                        logger.info("Received from %s: %r", peer, message.content)
                        await game.receive(message)
                    else:
                        game, result = await _run_command(message, game, config, send, games)
                        await send(result)
                        if isinstance(result, SessionResult) and game is not None:
                            # Only now that the client knows the session id is
                            # it safe for the cabin to speak first.
                            await game.open_channel()
                except CommandRefused as exc:
                    await send(ErrorResult(request_id=message.id, message=str(exc)))
    except ConnectionClosed:
        pass
    except Exception:
        logger.exception("Session for %s failed", peer)
        await connection.close(code=1011, reason="internal error")
    finally:
        logger.info("Client disconnected: %s", peer)


async def run_server(interface: str, port: int, config: ServerConfig) -> None:
    """Serve until interrupted."""
    handler = partial(handle_connection, config=config)
    async with serve(handler, interface, port):
        logger.info("Listening on ws://%s:%d", interface, port)
        await asyncio.get_running_loop().create_future()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    interface = args.interface if args.interface is not None else config.server.interface
    port = args.port if args.port is not None else config.server.port

    # Better to find out the games are missing now than when a player asks for
    # one, so this happens before the first connection rather than mid-session.
    try:
        ensure_games(GAMES_DIR)
    except DownloadError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    try:
        asyncio.run(run_server(interface, port, config.server))
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
