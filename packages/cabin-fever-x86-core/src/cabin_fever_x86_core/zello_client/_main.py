"""Entry point for the voice-only Cabin Fever x86 Zello client."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from elevenlabs.client import ElevenLabs
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from cabin_fever_x86_core import __version__
from cabin_fever_x86_core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86_core.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    ErrorResult,
    SessionInfo,
    UserMessage,
)
from cabin_fever_x86_core.session_client import SessionCommandError, open_session
from cabin_fever_x86_core.session_client import list_sessions as fetch_sessions
from cabin_fever_x86_core.sessions import ZELLO_CLIENT_COMPONENT
from cabin_fever_x86_core.transcripts import Transcript
from cabin_fever_x86_core.voice import VoiceError, synthesize, transcribe

logger = logging.getLogger(__name__)

LATEST = "latest"
ZELLO_TTS_FORMAT = "opus_48000_64"


class ZelloClientError(RuntimeError):
    """The Zello client could not load its configuration or carry voice traffic."""


def _resume_argument(value: str) -> UUID | str:
    """Read ``--resume`` as a session ID or the word ``latest``."""
    if value.lower() == LATEST:
        return LATEST
    try:
        return UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a session id: {value}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to Cabin Fever x86 through Zello.")
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Game server host; overrides client.host in the config.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Game server port; overrides client.port in the config.",
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
        help="List the sessions the game server holds and exit.",
    )
    return parser.parse_args(argv)


def _session_rows(sessions: Sequence[SessionInfo], where: str) -> list[str]:
    """Format a server session list for the command line."""
    rows = [f"{len(sessions)} session(s) on {where}, most recent first:", ""]
    rows.extend(
        f"  {info.session_id}  {info.modified.isoformat(timespec='seconds')}" for info in sessions
    )
    return rows


async def list_sessions(host: str, port: int) -> None:
    """Print the sessions held by the game server."""
    uri = f"ws://{host}:{port}"
    async with connect(uri) as connection:
        found = await fetch_sessions(connection)
    if not found:
        print("No sessions on the server yet.")
        return
    print("\n".join(_session_rows(found, uri)))


def load_credentials(path: str, credentials_type: Any) -> Any:
    """Load a zelpy credential object from a YAML file."""
    credentials_path = Path(path).expanduser()
    try:
        values = yaml.safe_load(credentials_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ZelloClientError(f"could not read credentials {credentials_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ZelloClientError(f"could not parse credentials {credentials_path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ZelloClientError(f"expected a YAML mapping in {credentials_path}")
    try:
        return credentials_type.from_mapping(values)
    except ValueError as exc:
        raise ZelloClientError(f"invalid credentials in {credentials_path}: {exc}") from exc


class ZelloBridge:
    """Relay authorized Zello voice messages over one game-server session."""

    def __init__(
        self,
        zello: Any,
        voice_message_type: type,
        upstream: ClientConnection,
        transcript: Transcript,
        voice: ElevenLabs,
        authorized_users: set[str],
    ) -> None:
        self.zello = zello
        self.voice_message_type = voice_message_type
        self.upstream = upstream
        self.transcript = transcript
        self.voice = voice
        self.authorized_users = {user.casefold() for user in authorized_users}

    async def run(self) -> None:
        """Pump both channels until either Zello or the game server closes."""
        tasks = [
            asyncio.create_task(coro, name=name)
            for name, coro in (
                ("receive-server", self._receive_server()),
                ("receive-zello", self._receive_zello()),
            )
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, ConnectionClosed):
                raise error

    async def _receive_server(self) -> None:
        """Synthesize every companion transmission and send it over Zello."""
        async for raw in self.upstream:
            try:
                message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
            except ValidationError:
                logger.warning("Discarding malformed game-server message: %r", raw)
                continue
            if isinstance(message, ErrorResult):
                self.transcript.log("error", message.request_id, message.message)
                logger.error("Game server: %s", message.message)
                continue
            if not isinstance(message, AssistantMessage):
                continue

            try:
                audio = await asyncio.to_thread(
                    synthesize,
                    self.voice,
                    message.content,
                    output_format=ZELLO_TTS_FORMAT,
                )
                if not audio.startswith(b"OggS"):
                    raise VoiceError("ElevenLabs returned Opus without an OGG container")
                clip = await asyncio.to_thread(
                    self.transcript.save_audio,
                    "clean",
                    message.id,
                    audio,
                    "ogg",
                )
                await self.zello.send_voice(audio)
            except (OSError, RuntimeError, ValueError, VoiceError) as exc:
                self.transcript.log("error", message.id, f"voice send failed: {exc}")
                logger.exception("Could not transmit companion message %s", message.id)
                continue
            self.transcript.log("assistant", message.id, message.content, clip)

    async def _receive_zello(self) -> None:
        """Transcribe authorized voice traffic; discard text and other users."""
        async for incoming in self.zello.messages():
            if not isinstance(incoming, self.voice_message_type):
                logger.debug("Ignoring non-voice Zello message")
                continue
            if incoming.sender.casefold() not in self.authorized_users:
                logger.warning(
                    "Ignoring Zello voice message from unauthorized user %r", incoming.sender
                )
                continue

            message = UserMessage(content="")
            clip = await asyncio.to_thread(
                self.transcript.save_audio,
                "player",
                message.id,
                incoming.audio,
                "ogg",
            )
            try:
                text = (
                    await asyncio.to_thread(
                        transcribe,
                        self.voice,
                        incoming.audio,
                        "zello-voice.ogg",
                        "audio/ogg",
                    )
                ).strip()
            except VoiceError as exc:
                self.transcript.log("error", message.id, f"transcription failed: {exc}", clip)
                logger.warning(
                    "Could not transcribe Zello message from %s: %s", incoming.sender, exc
                )
                continue
            if not text:
                self.transcript.log("error", message.id, "empty transcription", clip)
                logger.info("No speech found in Zello message from %s", incoming.sender)
                continue

            message.content = text
            self.transcript.log("user", message.id, text, clip)
            await self.upstream.send(message.model_dump_json())


async def run_client(
    host: str,
    port: int,
    credentials_path: str,
    channel: str,
    authorized_users: set[str],
    elevenlabs_api_key: str,
    resume: UUID | str | None = None,
) -> None:
    """Connect one Zello channel to one new or resumed game session."""
    try:
        from zelpy import VoiceMessage, Zello, ZelloCredentials
    except ImportError as exc:
        raise ZelloClientError(
            "Zello support is not installed; install cabin-fever-x86-core[zello]"
        ) from exc

    credentials = load_credentials(credentials_path, ZelloCredentials)
    uri = f"ws://{host}:{port}"
    async with connect(uri) as upstream:
        if resume == LATEST:
            found = await fetch_sessions(upstream)
            if not found:
                raise SessionCommandError("no sessions on the server to resume")
            resume = found[0].session_id
        if resume is not None and not isinstance(resume, UUID):
            raise SessionCommandError(f"not a session id: {resume}")

        session_id = await open_session(upstream, resume)
        transcript = Transcript(session_id, ZELLO_CLIENT_COMPONENT)
        verb = "resumed" if resume else "started"
        transcript.log("session", None, f"{verb} on {uri}, Zello channel {channel!r}")

        voice = ElevenLabs(api_key=elevenlabs_api_key)
        async with Zello(credentials, channel) as zello:
            print(
                f"Cabin Fever x86 (core version {__version__}) session {session_id}\n"
                f"Listening on Zello channel {channel!r}; press Ctrl-C to stop."
            )
            await ZelloBridge(
                zello,
                VoiceMessage,
                upstream,
                transcript,
                voice,
                authorized_users,
            ).run()


def main() -> None:
    """Load configuration and run the Zello transport."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
        if config.zello is None:
            raise ZelloClientError("config is missing the required zello section")
        if not config.client.elevenlabs_api_key:
            raise ZelloClientError("client.elevenlabs_api_key is required for the Zello client")
        if not config.zello.authorized_users:
            logger.warning("zello.authorized_users is empty; all incoming voice will be ignored")
        asyncio.run(
            run_client(
                host,
                port,
                config.zello.credentials_file,
                config.zello.channel,
                set(config.zello.authorized_users),
                config.client.elevenlabs_api_key,
                args.resume,
            )
        )
    except (OSError, ValueError, RuntimeError, WebSocketException, SessionCommandError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
