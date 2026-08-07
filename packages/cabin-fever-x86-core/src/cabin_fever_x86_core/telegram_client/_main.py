"""Entry point for the optional Cabin Fever x86 Telegram client."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID

from elevenlabs.client import ElevenLabs
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from cabin_fever_x86_core import __version__
from cabin_fever_x86_core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86_core.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    CompactionCompleted,
    CompactSessionCommand,
    ErrorResult,
    UserMessage,
)
from cabin_fever_x86_core.session_client import SessionCommandError, list_sessions, open_session
from cabin_fever_x86_core.sessions import TELEGRAM_CLIENT_COMPONENT
from cabin_fever_x86_core.transcripts import Transcript
from cabin_fever_x86_core.voice import VoiceError, synthesize, transcribe

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MESSAGE_MAX_AGE = 180
MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 120
TELEGRAM_TTS_FORMAT = "opus_48000_64"
STATE_PATH = Path("data") / TELEGRAM_CLIENT_COMPONENT / "sessions.json"


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text at natural boundaries into Telegram-sized messages."""
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n ")
    if text or not chunks:
        chunks.append(text)
    return chunks


def is_stale(message_date: datetime, now: datetime | None = None) -> bool:
    """Whether an update is old enough that replaying it would be surprising."""
    current = now or datetime.now(timezone.utc)
    return (current - message_date).total_seconds() > MESSAGE_MAX_AGE


def _load_state(path: Path = STATE_PATH) -> dict[int, UUID]:
    """Read the last server session used by each Telegram account."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(account): UUID(session) for account, session in raw.items()}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Could not read Telegram session state from %s", path)
        return {}


def _save_state(state: dict[int, UUID], path: Path = STATE_PATH) -> None:
    """Persist account-to-session associations atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({str(account): str(session) for account, session in state.items()}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@dataclass
class TelegramSession:
    """One Telegram account's open channel to the game server."""

    account_id: int
    chat_id: int
    session_id: UUID
    connection: ClientConnection
    transcript: Transcript
    pump: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    voice_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_compactions: dict[UUID, asyncio.Future[CompactionCompleted]] = field(
        default_factory=dict
    )
    has_replied: bool = False
    last_user_was_voice: bool = False


class TelegramBridge:
    """Translate Telegram updates and server wire messages in both directions."""

    def __init__(
        self,
        bot: Any,
        upstream_uri: str,
        allowed_accounts: set[int],
        voice: ElevenLabs | None = None,
    ) -> None:
        self.bot = bot
        self.upstream_uri = upstream_uri
        self.allowed_accounts = allowed_accounts
        self.voice = voice
        self.sessions: dict[int, TelegramSession] = {}
        self.last_sessions = _load_state()

    async def send(self, chat_id: int, text: str) -> None:
        """Send plain text, splitting messages Telegram would reject as too long."""
        for chunk in split_message(text):
            await self.bot.send_message(chat_id, chunk)

    async def close_session(self, account_id: int) -> None:
        """Close an account's current upstream channel, if any."""
        session = self.sessions.pop(account_id, None)
        if session is None:
            return
        if session.pump is not None:
            session.pump.cancel()
        await session.connection.close()
        if session.pump is not None:
            await asyncio.gather(session.pump, return_exceptions=True)

    async def close(self) -> None:
        """Close every upstream connection during bot shutdown."""
        await asyncio.gather(
            *(self.close_session(account) for account in list(self.sessions)),
            return_exceptions=True,
        )

    async def open(self, account_id: int, chat_id: int, resume: UUID | None) -> TelegramSession:
        """Replace the account's channel with a new or resumed server game."""
        await self.close_session(account_id)
        connection = await connect(self.upstream_uri)
        try:
            session_id = await open_session(connection, resume)
        except BaseException:
            await connection.close()
            raise

        transcript = Transcript(session_id, TELEGRAM_CLIENT_COMPONENT)
        verb = "resumed" if resume else "started"
        transcript.log("session", None, f"{verb} for Telegram account {account_id}")
        session = TelegramSession(account_id, chat_id, session_id, connection, transcript)
        self.sessions[account_id] = session
        self.last_sessions[account_id] = session_id
        _save_state(self.last_sessions)
        session.pump = asyncio.create_task(self._pump(session), name=f"telegram-{account_id}")
        logger.info("Telegram account %d %s session %s", account_id, verb, session_id)
        return session

    async def _pump(self, session: TelegramSession) -> None:
        """Forward unsolicited and solicited server transmissions to Telegram."""
        try:
            async for raw in session.connection:
                try:
                    message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
                except ValidationError:
                    logger.warning("Discarding malformed server message: %r", raw)
                    continue
                if isinstance(message, AssistantMessage):
                    await self._deliver_assistant(session, message)
                elif isinstance(message, CompactionCompleted):
                    pending = session.pending_compactions.pop(message.request_id, None)
                    if pending is not None and not pending.done():
                        pending.set_result(message)
                elif isinstance(message, ErrorResult):
                    pending = (
                        session.pending_compactions.pop(message.request_id, None)
                        if message.request_id is not None
                        else None
                    )
                    if pending is not None and not pending.done():
                        pending.set_exception(SessionCommandError(message.message))
                    else:
                        session.transcript.log("error", message.request_id, message.message)
                        await self.send(session.chat_id, f"Error: {message.message}")
        except ConnectionClosed as exc:
            logger.warning(
                "Server connection closed for Telegram account %d: %s", session.account_id, exc
            )
            await self.send(session.chat_id, "The game server connection closed.")
        except Exception:
            logger.exception("Telegram relay failed for account %d", session.account_id)
            with suppress(Exception):
                await self.send(session.chat_id, "The connection to the game failed.")
        finally:
            for pending in session.pending_compactions.values():
                if not pending.done():
                    pending.set_exception(SessionCommandError("game server connection closed"))
            session.pending_compactions.clear()
            if self.sessions.get(session.account_id) is session:
                self.sessions.pop(session.account_id, None)

    async def _deliver_assistant(self, session: TelegramSession, message: AssistantMessage) -> None:
        """Answer in kind after the first, which is voiced when possible."""
        clip: str | None = None
        display = message.content or "[static]"
        send_voice = (
            bool(message.content)
            and self.voice is not None
            and (not session.has_replied or session.last_user_was_voice)
        )
        session.has_replied = True

        if send_voice:
            async with session.voice_lock:
                try:
                    audio = await asyncio.to_thread(
                        synthesize,
                        self.voice,
                        message.content,
                        output_format=TELEGRAM_TTS_FORMAT,
                    )
                    if not audio.startswith(b"OggS"):
                        raise VoiceError("ElevenLabs returned Opus without an OGG container")
                    voice_note = BytesIO(audio)
                    voice_note.name = f"reply-{message.id}.ogg"
                    caption = (
                        message.content if len(message.content) <= MAX_CAPTION_LENGTH else None
                    )
                    await self.bot.send_file(
                        session.chat_id,
                        voice_note,
                        voice_note=True,
                        caption=caption,
                        parse_mode=None,
                    )
                except Exception:
                    logger.exception("Could not send Telegram voice reply %s", message.id)
                else:
                    try:
                        clip = await asyncio.to_thread(
                            session.transcript.save_audio,
                            "clean",
                            message.id,
                            audio,
                            "ogg",
                        )
                    except OSError:
                        logger.exception("Could not save Telegram voice reply %s", message.id)
                    if caption is None:
                        await self.send(session.chat_id, message.content)
                    session.transcript.log("assistant", message.id, message.content, clip)
                    return

        await self.send(session.chat_id, display)
        session.transcript.log("assistant", message.id, message.content, clip)

    async def _latest(self) -> UUID:
        """Return the server's most recently modified session."""
        async with connect(self.upstream_uri) as connection:
            found = await list_sessions(connection)
        if not found:
            raise SessionCommandError("no sessions on the server to resume")
        return found[0].session_id

    async def handle(self, event: Any) -> None:
        """Authorize and process one incoming Telegram text or voice update."""
        account_id = event.sender_id
        chat_id = event.chat_id
        sender = await event.get_sender()
        username = getattr(sender, "username", None)

        if account_id not in self.allowed_accounts:
            logger.warning(
                "Rejected Telegram connection: user_id=%s username=@%s chat_id=%s",
                account_id,
                username or "<none>",
                chat_id,
            )
            await event.respond("Not authorized.")
            return
        if not event.is_private:
            logger.warning(
                "Rejected non-private Telegram chat: user_id=%s username=@%s chat_id=%s",
                account_id,
                username or "<none>",
                chat_id,
            )
            await event.respond("Please message me in a private chat.")
            return
        if is_stale(event.message.date):
            logger.info("Skipping stale Telegram message from user_id=%s", account_id)
            return

        voice_note = event.message.voice
        text = (event.message.text or "").strip()
        if voice_note is None and not text:
            return
        logger.info(
            "Telegram message from user_id=%d username=@%s", account_id, username or "<none>"
        )

        try:
            async with self.bot.action(chat_id, "typing"):
                if voice_note is not None:
                    await self._handle_voice(account_id, chat_id, event.message)
                else:
                    await self._handle_text(account_id, chat_id, text)
        except (OSError, WebSocketException, SessionCommandError, ValueError) as exc:
            logger.exception("Could not connect Telegram account %d to the game", account_id)
            await self.send(chat_id, f"Could not connect to the game: {exc}")
        except Exception as exc:
            logger.exception("Telegram handler failed for account %d", account_id)
            await self.send(chat_id, f"Telegram client error: {type(exc).__name__}: {exc}")

    async def _ensure_session(self, account_id: int, chat_id: int) -> TelegramSession:
        """Return the account's channel, lazily starting or resuming it."""
        session = self.sessions.get(account_id)
        if session is not None:
            return session

        resume = self.last_sessions.get(account_id)
        session = await self.open(account_id, chat_id, resume)
        await self.send(
            chat_id,
            f"{'Resumed' if resume else 'Started'} session {session.session_id}.",
        )
        return session

    async def _handle_voice(self, account_id: int, chat_id: int, message: Any) -> None:
        """Transcribe one Telegram voice note and forward its text silently."""
        if self.voice is None:
            await self.send(chat_id, "Voice transcription is not configured.")
            return

        media = message.file
        size = getattr(media, "size", None)
        duration = getattr(media, "duration", None)
        if size is not None and size > MAX_VOICE_BYTES:
            await self.send(chat_id, "That voice message is too large (10 MB maximum).")
            return
        if duration is not None and duration > MAX_VOICE_SECONDS:
            await self.send(chat_id, "That voice message is too long (2 minutes maximum).")
            return

        session = await self._ensure_session(account_id, chat_id)
        async with session.lock:
            audio = await message.download_media(file=bytes)
            if not isinstance(audio, bytes) or not audio:
                await self.send(chat_id, "I couldn't download that voice message.")
                return
            if len(audio) > MAX_VOICE_BYTES:
                await self.send(chat_id, "That voice message is too large (10 MB maximum).")
                return

            user_message = UserMessage(content="")
            clip = await asyncio.to_thread(
                session.transcript.save_audio,
                "player",
                user_message.id,
                audio,
                "ogg",
            )
            mimetype = getattr(media, "mime_type", None) or "audio/ogg"
            try:
                text = (
                    await asyncio.to_thread(
                        transcribe,
                        self.voice,
                        audio,
                        "telegram-voice.ogg",
                        mimetype,
                    )
                ).strip()
            except VoiceError as exc:
                logger.warning("Could not transcribe Telegram voice message: %s", exc)
                session.transcript.log(
                    "error", user_message.id, f"transcription failed: {exc}", clip
                )
                await self.send(chat_id, "I couldn't transcribe that voice message.")
                return

            if not text:
                session.transcript.log("error", user_message.id, "empty transcription", clip)
                await self.send(chat_id, "I couldn't make out any speech in that message.")
                return

            user_message.content = text
            session.last_user_was_voice = True
            session.transcript.log("user", user_message.id, text, clip)
            await session.connection.send(user_message.model_dump_json())

    async def _handle_text(self, account_id: int, chat_id: int, text: str) -> None:
        """Execute a command or forward ordinary text to the open game."""
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower() if command.startswith("/") else ""

        if command in {"/help", "/start"}:
            if command == "/start":
                session = await self.open(account_id, chat_id, None)
                await self.send(chat_id, f"Started session {session.session_id}.")
            await self.send(
                chat_id,
                "/start — start a new game\n"
                "/resume [session-id] — resume a game (latest if omitted)\n"
                "/continue — continue the most recent game\n"
                "/sessions — list saved games\n"
                "/session — show the current game\n"
                "/compact — compact the current conversation\n"
                "/quit — close the channel",
            )
            return
        if command == "/quit":
            await self.close_session(account_id)
            await self.send(chat_id, "Disconnected from the game.")
            return
        if command == "/session":
            session = self.sessions.get(account_id)
            await self.send(
                chat_id, f"Session {session.session_id}." if session else "No game is open."
            )
            return
        if command == "/compact":
            session = self.sessions.get(account_id)
            if session is None:
                await self.send(chat_id, "No game is open.")
                return
            async with session.lock:
                compact = CompactSessionCommand()
                completed = asyncio.get_running_loop().create_future()
                session.pending_compactions[compact.id] = completed
                try:
                    await session.connection.send(compact.model_dump_json())
                    await completed
                finally:
                    session.pending_compactions.pop(compact.id, None)
            await self.send(chat_id, "Compaction completed.")
            return
        if command == "/sessions":
            async with connect(self.upstream_uri) as connection:
                found = await list_sessions(connection)
            text = (
                "No sessions on the server."
                if not found
                else "Sessions:\n"
                + "\n".join(
                    f"{info.session_id}  {info.modified.isoformat(timespec='seconds')}"
                    for info in found
                )
            )
            await self.send(chat_id, text)
            return
        if command == "/resume":
            resume = UUID(argument.strip()) if argument.strip() else await self._latest()
            session = await self.open(account_id, chat_id, resume)
            await self.send(chat_id, f"Resumed session {session.session_id}.")
            return
        if command == "/continue":
            resume = await self._latest()
            session = await self.open(account_id, chat_id, resume)
            await self.send(chat_id, f"Resumed session {session.session_id}.")
            return
        if command:
            await self.send(chat_id, "Unknown command. Try /help.")
            return

        session = await self._ensure_session(account_id, chat_id)

        async with session.lock:
            message = UserMessage(content=text)
            session.last_user_was_voice = False
            session.transcript.log("user", message.id, text)
            await session.connection.send(message.model_dump_json())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to Cabin Fever x86 through Telegram.")
    parser.add_argument(
        "--config", default=None, help=f"Config file (default: {DEFAULT_CONFIG_PATH})."
    )
    parser.add_argument("--host", default=None, help="Game server host; overrides client.host.")
    parser.add_argument(
        "--port", type=int, default=None, help="Game server port; overrides client.port."
    )
    return parser.parse_args(argv)


async def run_bot(
    api_id: int,
    api_hash: str,
    token: str,
    uri: str,
    allowed: set[int],
    elevenlabs_api_key: str | None,
) -> None:
    """Start Telethon and relay updates until it disconnects."""
    try:
        from telethon import TelegramClient, events
    except ImportError as exc:
        raise RuntimeError(
            "Telegram support is not installed; install cabin-fever-x86-core[telegram]"
        ) from exc

    bot = TelegramClient("cf86-telegram", api_id, api_hash)
    await bot.start(bot_token=token)
    voice = ElevenLabs(api_key=elevenlabs_api_key) if elevenlabs_api_key else None
    if voice is None:
        logger.warning("No ElevenLabs key: Telegram voice messages will be rejected.")
    bridge = TelegramBridge(bot, uri, allowed, voice)
    bot.add_event_handler(bridge.handle, events.NewMessage(incoming=True))
    identity = await bot.get_me()
    logger.info(
        "Telegram bot @%s connected; allowed user IDs: %s", identity.username, sorted(allowed)
    )
    try:
        await bot.run_until_disconnected()
    finally:
        await bridge.close()
        await bot.disconnect()


def main() -> None:
    """Load configuration and run the Telegram transport."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    telegram = config.telegram_client
    missing = [
        name
        for name, value in (
            ("bot_token", telegram.bot_token),
            ("api_id", telegram.api_id),
            ("api_hash", telegram.api_hash),
        )
        if value is None
    ]
    if missing:
        print(f"error: telegram_client is missing: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)
    if not telegram.allowed_accounts:
        logger.warning(
            "telegram_client.allowed_accounts is empty; all users will be rejected. "
            "Send the bot a message and read user_id from this log."
        )

    host = args.host if args.host is not None else config.client.host
    port = args.port if args.port is not None else config.client.port
    print(f"Cabin Fever x86 Telegram client (core version {__version__})")
    try:
        asyncio.run(
            run_bot(
                telegram.api_id,
                telegram.api_hash,
                telegram.bot_token,
                f"ws://{host}:{port}",
                set(telegram.allowed_accounts),
                config.client.elevenlabs_api_key,
            )
        )
    except (OSError, RuntimeError) as exc:
        logger.error("Could not start Telegram client: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
