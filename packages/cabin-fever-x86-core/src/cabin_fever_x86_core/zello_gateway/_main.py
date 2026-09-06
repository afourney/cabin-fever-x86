"""Entry point for the voice-only Cabin Fever x86 Zello gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import soundfile as sf
import yaml
from elevenlabs.client import ElevenLabs
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from cabin_fever_x86_core import __version__
from cabin_fever_x86_core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86_core.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    ErrorResult,
    UserMessage,
)
from cabin_fever_x86_core.session_client import SessionCommandError, open_session
from cabin_fever_x86_core.sessions import ZELLO_GATEWAY_COMPONENT, user_dir
from cabin_fever_x86_core.transcripts import Transcript
from cabin_fever_x86_core.voice import VoiceError, synthesize, transcribe

logger = logging.getLogger(__name__)

ZELLO_TTS_FORMAT = "opus_48000_64"
STATIC_SECONDS = 0.22
STATIC_SAMPLE_RATE = 48_000


def _static_burst() -> bytes:
    """Make a short Ogg Opus burst for a keyed-up but silent transmission."""
    frames = round(STATIC_SECONDS * STATIC_SAMPLE_RATE)
    samples = np.random.default_rng().uniform(-0.18, 0.18, frames).astype(np.float32)
    # Take the hard edge off the generated clip; Zello supplies the channel's
    # own key-up and tail around it.
    ramp = min(round(0.01 * STATIC_SAMPLE_RATE), frames // 2)
    envelope = np.ones(frames, dtype=np.float32)
    envelope[:ramp] = np.linspace(0, 1, ramp, dtype=np.float32)
    envelope[-ramp:] = np.linspace(1, 0, ramp, dtype=np.float32)

    encoded = BytesIO()
    sf.write(
        encoded,
        samples * envelope,
        STATIC_SAMPLE_RATE,
        format="OGG",
        subtype="OPUS",
    )
    return encoded.getvalue()


class ZelloGatewayError(RuntimeError):
    """The Zello gateway could not load its configuration or carry voice traffic."""


def _unique_channels(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    channels: dict[str, Any] = {}
    for channel, value in pairs:
        if channel in channels:
            raise ValueError(f"duplicate channel {channel!r}")
        channels[channel] = value
    return channels


class ChannelSessions:
    """One shared state writer per owner; updates never yield between read and save."""

    def __init__(self, user_id: str) -> None:
        """Load and validate the owner's associations without fallback on corrupt state."""
        self.path = user_dir(user_id) / ZELLO_GATEWAY_COMPONENT / "sessions.json"
        try:
            if self.path.with_suffix(".tmp").exists():
                raise ValueError("unfinished session state write; inspect sessions.tmp")
            raw = json.loads(
                self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_channels
            )
            if not isinstance(raw, dict):
                raise ValueError("expected a channel-to-session mapping")
            self.sessions: dict[str, UUID] = {}
            for channel, session in raw.items():
                if not channel or channel != channel.strip() or not isinstance(session, str):
                    raise ValueError("expected channel names and session UUID strings")
                self.sessions[channel] = UUID(session)
            if len(set(self.sessions.values())) != len(self.sessions):
                raise ValueError("channels must have independent sessions")
        except FileNotFoundError:
            self.sessions = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ZelloGatewayError(
                f"could not read Zello session state {self.path}: {exc}"
            ) from exc

    def remember(self, channel: str, session_id: UUID) -> None:
        """Atomically save a complete snapshot before publishing the in-memory update."""
        updated = {**self.sessions, channel: session_id}
        if len(set(updated.values())) != len(updated):
            raise ZelloGatewayError("the server returned a session already used by another channel")
        temporary = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({key: str(value) for key, value in updated.items()}, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        except OSError as exc:
            raise ZelloGatewayError(
                f"could not save Zello session state {self.path}: {exc}"
            ) from exc
        self.sessions = updated


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
    return parser.parse_args(argv)


def load_credentials(path: str, credentials_type: Any) -> Any:
    """Load a zelpy credential object from a YAML file."""
    credentials_path = Path(path).expanduser()
    try:
        values = yaml.safe_load(credentials_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ZelloGatewayError(f"could not read credentials {credentials_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ZelloGatewayError(f"could not parse credentials {credentials_path}: {exc}") from exc
    if not isinstance(values, dict):
        raise ZelloGatewayError(f"expected a YAML mapping in {credentials_path}")
    try:
        return credentials_type.from_mapping(values)
    except ValueError as exc:
        raise ZelloGatewayError(f"invalid credentials in {credentials_path}: {exc}") from exc


class ZelloGateway:
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

        async def receive_server() -> None:
            await self._receive_server()
            raise ZelloGatewayError("game server connection closed")

        async def receive_zello() -> None:
            await self._receive_zello()
            raise ZelloGatewayError("Zello connection closed")

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(receive_server(), name="receive-server")
            tasks.create_task(receive_zello(), name="receive-zello")

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
                if message.content:
                    audio = await asyncio.to_thread(
                        synthesize,
                        self.voice,
                        message.content,
                        output_format=ZELLO_TTS_FORMAT,
                    )
                else:
                    audio = await asyncio.to_thread(_static_burst)
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


def _zello_api() -> tuple[Any, Any, Any]:
    """Load optional Zello support, with installation instructions on failure."""
    try:
        from zelpy import VoiceMessage, Zello, ZelloCredentials
    except ImportError as exc:
        raise ZelloGatewayError(
            "Zello support is unavailable in this Python environment.\n\n"
            "Install the optional dependencies:\n"
            "  python -m pip install 'cabin-fever-x86-core[zello]'\n\n"
            "Or, from a repository checkout, run:\n"
            "  uv run --extra zello cf86-zello"
        ) from exc
    return VoiceMessage, Zello, ZelloCredentials


async def run_gateway(
    host: str,
    port: int,
    credentials_path: str,
    channel: str,
    authorized_users: set[str],
    elevenlabs_api_key: str,
    *,
    user_id: str,
    sessions: ChannelSessions,
) -> None:
    """Connect one channel, resuming only its saved session under its owner."""
    VoiceMessage, Zello, ZelloCredentials = _zello_api()

    credentials = load_credentials(credentials_path, ZelloCredentials)
    uri = f"ws://{host}:{port}"
    async with connect(uri, additional_headers={"X-CF86-User-ID": user_id}) as upstream:
        resume = sessions.sessions.get(channel)
        session_id = await open_session(upstream, resume)
        if resume is not None and session_id != resume:
            raise ZelloGatewayError(
                f"server resumed {session_id} instead of saved session {resume}"
            )
        sessions.remember(channel, session_id)
        transcript = Transcript(session_id, ZELLO_GATEWAY_COMPONENT, user_id=user_id)
        verb = "resumed" if resume else "started"
        transcript.log("session", None, f"{verb} on {uri}, Zello channel {channel!r}")

        voice = ElevenLabs(api_key=elevenlabs_api_key)
        async with Zello(credentials, channel) as zello:
            print(
                f"Cabin Fever x86 (core version {__version__}) session {session_id}\n"
                f"Listening on Zello channel {channel!r} as user {user_id!r}; "
                "press Ctrl-C to stop."
            )
            await ZelloGateway(
                zello,
                VoiceMessage,
                upstream,
                transcript,
                voice,
                authorized_users,
            ).run()


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_error_message(error) for error in exc.exceptions)
    return str(exc)


async def run_channels(
    host: str,
    port: int,
    credentials_path: str,
    channels: dict[str, tuple[str, set[str]]],
    elevenlabs_api_key: str,
) -> None:
    """Supervise all channels together; any failure closes every connection."""
    if not channels:
        raise ZelloGatewayError("No Zello identities in users; no channels configured")
    # Validate every owner's state before any channel can start a new game.
    states = {owner: ChannelSessions(owner) for owner in {value[0] for value in channels.values()}}

    async def run_channel(channel: str, owner: str, contributors: set[str]) -> None:
        try:
            await run_gateway(
                host,
                port,
                credentials_path,
                channel,
                contributors,
                elevenlabs_api_key,
                user_id=owner,
                sessions=states[owner],
            )
            raise ZelloGatewayError("channel relay stopped")
        except Exception as exc:
            raise ZelloGatewayError(
                f"Zello channel {channel!r} (user {owner!r}): {_error_message(exc)}"
            ) from exc

    async with asyncio.TaskGroup() as tasks:
        for channel, (owner, contributors) in channels.items():
            tasks.create_task(run_channel(channel, owner, contributors), name=f"zello-{channel}")


def main() -> None:
    """Load configuration and run the Zello gateway."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    try:
        config = load_config(args.config)
    except (ConfigError, ZelloGatewayError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    host = args.host if args.host is not None else config.client.host
    port = args.port if args.port is not None else config.client.port
    try:
        if config.zello is None:
            raise ZelloGatewayError("config is missing the required zello section")
        channels = config.zello_channels()
        if not channels:
            raise ZelloGatewayError("No Zello identities in users; no channels configured")
        _zello_api()
        if not config.client.elevenlabs_api_key:
            raise ZelloGatewayError("client.elevenlabs_api_key is required for the Zello gateway")
        asyncio.run(
            run_channels(
                host,
                port,
                config.zello.credentials_file,
                channels,
                config.client.elevenlabs_api_key,
            )
        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        WebSocketException,
        SessionCommandError,
        ExceptionGroup,
    ) as exc:
        print(f"error: {_error_message(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
