"""Entry point for the Cabin Fever x86 radio client.

Same session protocol as the text client; the difference is what carries the
words. Hold SPACE to transmit: the take is transcribed, sent as a
:class:`UserMessage`, and whatever comes back is spoken in the companion's
voice and put over the airwaves before it reaches the speakers.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import UUID, uuid4

import pygame
from elevenlabs.client import ElevenLabs
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from cabin_fever_x86.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    ErrorResult,
    UserMessage,
)
from cabin_fever_x86.radio_client._audio import (
    MIXER_RATE,
    PushToTalkRecorder,
    RadioPlayer,
    describe_output,
    over_the_air,
)
from cabin_fever_x86.session_client import (
    SessionCommandError,
    open_session,
)
from cabin_fever_x86.session_client import (
    list_sessions as fetch_sessions,
)
from cabin_fever_x86.sessions import RADIO_CLIENT_COMPONENT
from cabin_fever_x86.transcripts import Transcript
from cabin_fever_x86.voice import TTS_SUFFIX, synthesize, transcribe

logger = logging.getLogger(__name__)

WINDOW_SIZE = (500, 180)
FRAME_RATE = 60


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Talk to Cabin Fever x86 over the radio.")
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--resume",
        metavar="SESSION_ID",
        type=UUID,
        default=None,
        help="Resume an existing session instead of starting a new one.",
    )
    group.add_argument(
        "--list-sessions",
        action="store_true",
        help="List the sessions the server holds and exit.",
    )
    return parser.parse_args(argv)


async def _open_session(connection: ClientConnection, resume: UUID | None) -> Transcript:
    """Ask the server to start or resume a game, and open the matching transcript."""
    session_id = await open_session(connection, resume)
    transcript = Transcript(session_id, RADIO_CLIENT_COMPONENT)
    verb = "Resumed" if resume else "Started"
    print(f"{verb} session {session_id}, logging to {transcript.path}")
    transcript.log("session", None, f"{verb.lower()} on {connection.remote_address}")
    return transcript


async def list_sessions(host: str, port: int) -> None:
    """Print the sessions the server holds data for."""
    uri = f"ws://{host}:{port}"
    async with connect(uri) as connection:
        found = await fetch_sessions(connection)

    if not found:
        print("No sessions on the server yet.")
        return
    print(f"{len(found)} session(s) on {uri}, most recent first:")
    for info in found:
        print(f"  {info.session_id}  {info.modified.isoformat(timespec='seconds')}")


class Radio:
    """One radio session: the set, the connection, and the traffic between them.

    Slow work runs on threads so the pygame loop never stalls, and each
    direction is drained by a single worker so takes and replies keep the
    order they were made in.
    """

    def __init__(
        self,
        connection: ClientConnection,
        transcript: Transcript,
        voice: ElevenLabs | None,
    ) -> None:
        self._connection = connection
        self._transcript = transcript
        self._voice = voice
        self._takes: asyncio.Queue[tuple[int, UUID, bytes]] = asyncio.Queue()
        self._replies: asyncio.Queue[AssistantMessage] = asyncio.Queue()
        self.recorder = PushToTalkRecorder()
        self.player = RadioPlayer()
        self.heard = ""
        self.in_flight = 0

    def transmit(self, take: tuple[int, bytes]) -> None:
        """Hand a finished take to the transcriber.

        The id the transmission will carry is minted here, so the recording on
        disk is named after the message it becomes.
        """
        number, wav = take
        self.in_flight += 1
        self._takes.put_nowait((number, uuid4(), wav))

    async def run_uplink(self) -> None:
        """Transcribe each take in turn and send it on as a transmission.

        One take that cannot be transcribed loses that take and nothing more;
        the mic keeps working, and the recording is kept either way.
        """
        while True:
            take, message_id, wav = await self._takes.get()
            try:
                clip = await asyncio.to_thread(
                    self._transcript.save_audio, "player", message_id, wav, "wav"
                )
                if self._voice is None:
                    self.heard = "[no ElevenLabs key: nothing to transcribe with]"
                    continue
                text = (
                    await asyncio.to_thread(
                        transcribe, self._voice, wav, f"take_{take:03d}.wav", "audio/wav"
                    )
                ).strip()
            except Exception:
                logger.exception("Take %d could not be transcribed", take)
                self.heard = f"[take {take} failed]"
                continue
            finally:
                self.in_flight -= 1

            if not text:
                self.heard = "(silence)"
                continue

            self.heard = text
            message = UserMessage(id=message_id, content=text)
            self._transcript.log("user", message.id, text, clip)
            # A send that fails means the connection is gone; let it end the session.
            await self._connection.send(message.model_dump_json())

    async def run_downlink(self) -> None:
        """Read the server's transmissions and queue them to be spoken."""
        async for raw in self._connection:
            try:
                message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
            except ValidationError:
                logger.warning("Discarding malformed message: %r", raw)
                continue
            if isinstance(message, ErrorResult):
                self._transcript.log("error", message.request_id, message.message)
                logger.warning("Server: %s", message.message)
                continue
            if not isinstance(message, AssistantMessage):
                continue  # a late answer to a session command
            print(f"\n{message.content}")
            self.in_flight += 1
            # Logged once it has been spoken, so the record can name the clip.
            self._replies.put_nowait(message)

    async def run_voice(self) -> None:
        """Speak each reply in turn, over the airwaves, and record it.

        A line that cannot be synthesised, processed, or decoded is dropped
        with a warning — letting it escape would take the voice down for the
        rest of the session. It still reaches the transcript either way; only
        the clip is missing.
        """
        while True:
            message = await self._replies.get()
            clip: str | None = None
            try:
                if self._voice is not None:
                    clean = await asyncio.to_thread(synthesize, self._voice, message.content)
                    clip = await asyncio.to_thread(
                        self._transcript.save_audio, "clean", message.id, clean, TTS_SUFFIX
                    )
                    audio = await asyncio.to_thread(over_the_air, clean)
                    if audio is not clean:  # the effect ran; keep what was heard
                        clip = await asyncio.to_thread(
                            self._transcript.save_audio, "radio", message.id, audio, "wav"
                        )
                    self.player.enqueue(audio)
            except Exception:
                logger.exception("Could not speak %s", message.id)
            finally:
                self.in_flight -= 1
                self._transcript.log("assistant", message.id, message.content, clip)

    def status(self) -> str:
        if self.recorder.recording:
            return "TRANSMITTING..."
        if self.player.busy:
            return "RECEIVING..."
        if self.in_flight:
            return f"DECODING... ({self.in_flight})"
        return "Hold SPACE to transmit"

    def close(self) -> None:
        self.recorder.close()
        self.player.stop()


async def _run_ui(radio: Radio) -> None:
    """Pump pygame at a steady frame rate without blocking the event loop."""
    screen = pygame.display.set_mode(WINDOW_SIZE)
    pygame.display.set_caption("Cabin Fever x86 — hold SPACE to talk")
    font = pygame.font.Font(None, 36)
    frame = 1 / FRAME_RATE
    space_is_down = False

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return

            if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE:
                if not space_is_down:  # ignore keyboard auto-repeat
                    space_is_down = True
                    # Half-duplex: keying the mic cuts the companion off.
                    radio.player.interrupt()
                    radio.recorder.start()

            # Releasing SPACE ends the take. Losing focus does too, so the mic
            # cannot stay keyed open after switching windows.
            elif space_is_down and (
                (event.type == pygame.KEYUP and event.key == pygame.K_SPACE)
                or event.type == pygame.WINDOWFOCUSLOST
            ):
                space_is_down = False
                if take := radio.recorder.stop():
                    radio.transmit(take)

        radio.recorder.update()
        # Holding the reply until the mic closes keeps the speakers out of the
        # next recording.
        radio.player.update(mic_is_hot=radio.recorder.recording)

        screen.fill("black")
        center = screen.get_rect().center
        label = font.render(radio.status(), True, "white")
        screen.blit(label, label.get_rect(center=(center[0], center[1] - 25)))
        if radio.heard:
            heard = font.render(_ellipsize(radio.heard, 44), True, "grey")
            screen.blit(heard, heard.get_rect(center=(center[0], center[1] + 25)))
        pygame.display.flip()

        await asyncio.sleep(frame)


async def run_client(host: str, port: int, api_key: str | None, resume: UUID | None = None) -> None:
    """Connect, open a session, and hold the channel until the window closes."""
    uri = f"ws://{host}:{port}"
    print(f"Connecting to {uri} ...")

    voice = ElevenLabs(api_key=api_key) if api_key else None
    if voice is None:
        print("No ElevenLabs API key; the radio will be silent in both directions.")

    async with connect(uri) as connection:
        transcript = await _open_session(connection, resume)

        pygame.mixer.pre_init(frequency=MIXER_RATE)
        pygame.init()
        logger.info("Audio out: %s", describe_output())
        radio = Radio(connection, transcript, voice)
        radio.recorder.stream.start()

        workers = [
            asyncio.create_task(coro)
            for coro in (radio.run_uplink(), radio.run_downlink(), radio.run_voice())
        ]
        ui = asyncio.create_task(_run_ui(radio))

        try:
            done, pending = await asyncio.wait([ui, *workers], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None and not isinstance(error, ConnectionClosed):
                    raise error
        finally:
            radio.close()
            pygame.quit()


def _ellipsize(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
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
        asyncio.run(run_client(host, port, config.client.elevenlabs_api_key, args.resume))
    except SessionCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (OSError, WebSocketException) as exc:
        print(f"error: could not connect to ws://{host}:{port}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass
    print("\nDisconnected.")


if __name__ == "__main__":
    main()
