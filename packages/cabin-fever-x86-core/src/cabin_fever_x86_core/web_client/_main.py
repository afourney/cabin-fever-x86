"""Entry point for the Cabin Fever x86 web client.

Serves a page that holds the radio, and relays between it and the game server.
The browser captures and plays audio; this process keeps the ElevenLabs key,
does the transcription and the speech, and writes the session's record.

    uv run cf86-web
    open http://127.0.0.1:8000

Audio never travels as JSON: a finished take is POSTed as a blob, and replies
stream as PCM over the websocket. Completed recordings remain at ``/audio/...``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import tempfile
import wave
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from elevenlabs.client import AsyncElevenLabs, ElevenLabs
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from cabin_fever_x86_core import __version__
from cabin_fever_x86_core.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86_core.messages import (
    SERVER_MESSAGE_ADAPTER,
    AssistantMessage,
    ErrorResult,
    UserMessage,
)
from cabin_fever_x86_core.session_client import (
    SessionCommandError,
    list_sessions,
    open_session,
)
from cabin_fever_x86_core.sessions import WEB_CLIENT_COMPONENT, session_dir
from cabin_fever_x86_core.transcripts import AUDIO_DIR, Transcript
from cabin_fever_x86_core.voice import PCM_SAMPLE_RATE, VoiceError, stream_speech, transcribe

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")

# The artwork the page opens on, as it sits in a checkout.
SPLASH_ART = Path("imgs/cabin-fever-x86__16x9.png")

# Where to look for a rain recording before falling back to the packaged one.
AMBIENCE_DIR = Path("audio")

# What the browser's MediaRecorder is likely to hand us, by content type.
SUFFIXES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


@dataclass
class Radio:
    """One browser tab holding one game session open."""

    session_id: UUID
    upstream: ClientConnection
    transcript: Transcript
    browser: WebSocket
    stream_id: int = 0


async def _stream_reply(
    radio: Radio, message: AssistantMessage, voice: AsyncElevenLabs
) -> str | None:
    """Forward PCM immediately, and keep a WAV only after synthesis succeeds.

    Wire protocol: audio_start describes pcm_s16le, sample_rate and channels.
    Every binary message is a big-endian uint32 stream ID followed by PCM.
    audio_end closes reception (status complete/error), not browser playback.
    """
    radio.stream_id += 1
    stream_id = radio.stream_id
    await radio.browser.send_json(
        {
            "type": "audio_start",
            "id": str(message.id),
            "stream_id": stream_id,
            "format": "pcm_s16le",
            "sample_rate": PCM_SAMPLE_RATE,
            "channels": 1,
        }
    )
    temporary: Path | None = None
    status = "error"
    clip = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=radio.transcript.audio_dir, suffix=".part", delete=False
        ) as output:
            temporary = Path(output.name)
            with wave.open(output, "wb") as recording:
                recording.setnchannels(1)
                recording.setsampwidth(2)
                recording.setframerate(PCM_SAMPLE_RATE)
                pending = b""
                size = 0
                async with aclosing(stream_speech(voice, message.content)) as audio:
                    async for chunk in audio:
                        pending += chunk
                        length = len(pending) // 2 * 2
                        if not length:
                            continue
                        samples, pending = pending[:length], pending[length:]
                        # Await each send: a slow connection must not build an
                        # unbounded server-side queue of generated audio.
                        await radio.browser.send_bytes(stream_id.to_bytes(4, "big") + samples)
                        recording.writeframesraw(samples)
                        size += length
                if pending or not size:
                    raise VoiceError("incomplete or empty PCM stream")
        path = radio.transcript.audio_dir / f"clean_{message.id}.wav"
        temporary.replace(path)
        clip = str(path.relative_to(radio.transcript.dir))
        status = "complete"
    except (VoiceError, OSError) as exc:
        logger.warning("Could not speak %s: %s", message.id, exc)
        await radio.browser.send_json(
            {"type": "error", "text": "Voice playback failed. The reply is available as text."}
        )
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    await radio.browser.send_json({"type": "audio_end", "stream_id": stream_id, "status": status})
    return clip


async def _pump(radio: Radio, voice: AsyncElevenLabs | None) -> None:
    """Relay text immediately, then stream each reply's voice in order."""
    async for raw in radio.upstream:
        try:
            message = SERVER_MESSAGE_ADAPTER.validate_json(raw)
        except ValidationError:
            logger.warning("Discarding malformed message: %r", raw)
            continue
        if isinstance(message, ErrorResult):
            radio.transcript.log("error", message.request_id, message.message)
            await radio.browser.send_json({"type": "error", "text": message.message})
            continue
        if not isinstance(message, AssistantMessage):
            continue
        await radio.browser.send_json(
            {"type": "assistant", "id": str(message.id), "text": message.content}
        )
        clip = None
        try:
            if voice is not None and message.content:
                clip = await _stream_reply(radio, message, voice)
        finally:
            radio.transcript.log("assistant", message.id, message.content, clip)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the Cabin Fever x86 web client.")
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Game server to connect to. Overrides client.host in the config.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Game server port. Overrides client.port in the config.",
    )
    parser.add_argument("--web-host", default="127.0.0.1", help="Interface to serve the page on.")
    parser.add_argument("--web-port", type=int, default=8000, help="Port to serve the page on.")
    return parser.parse_args(argv)


def create_app(upstream_uri: str, api_key: str | None) -> FastAPI:
    """Build the web app. *upstream_uri* is the game server's websocket."""
    voice: ElevenLabs | None = None
    streaming_voice: AsyncElevenLabs | None = None
    if not api_key:
        logger.warning("No ElevenLabs key: the radio will be text-only in both directions.")

    live: dict[UUID, Radio] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal voice, streaming_voice
        with httpx.Client(timeout=60) as sync_http:
            async with httpx.AsyncClient(timeout=60) as async_http:
                if api_key:
                    voice = ElevenLabs(api_key=api_key, httpx_client=sync_http)
                    streaming_voice = AsyncElevenLabs(api_key=api_key, httpx_client=async_http)
                logger.info("Ready.")
                try:
                    yield
                finally:
                    for radio in list(live.values()):
                        await radio.upstream.close()

    app = FastAPI(title="Cabin Fever x86", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/pcm-player.js")
    async def pcm_player() -> FileResponse:
        """Serve the provider-independent streaming audio player."""
        return FileResponse(STATIC_DIR / "pcm-player.js", media_type="text/javascript")

    @app.get("/splash")
    async def splash() -> FileResponse:
        """Serve the cabin, for the page to open on.

        Prefer the full-size art when running from a checkout, so editing it
        shows up without regenerating anything; fall back to the copy that
        ships in the package.
        """
        for candidate in (SPLASH_ART, STATIC_DIR / "splash.jpg"):
            if candidate.is_file():
                return FileResponse(candidate)
        raise HTTPException(status_code=404, detail="no splash art")

    @app.get("/background")
    async def background() -> FileResponse:
        """Serve the rainy landscape behind the radio interface."""
        return FileResponse(STATIC_DIR / "background_16x9.png")

    @app.get("/ambience")
    async def ambience() -> FileResponse:
        """Serve a rain recording to play under the page, if there is one.

        Looked for as ``rain.<ext>`` beside the working directory first, so a
        file can be dropped in without reinstalling, then in the package. A
        404 is the normal answer: the page makes its own rain instead.
        """
        for folder in (AMBIENCE_DIR, STATIC_DIR):
            for candidate in sorted(folder.glob("rain.*")):
                if candidate.is_file():
                    return FileResponse(candidate)
        raise HTTPException(status_code=404, detail="no ambience; synthesise it")

    @app.get("/sessions")
    async def sessions() -> dict:
        """List what the game server has on file, for the resume list."""
        try:
            async with connect(upstream_uri) as connection:
                found = await list_sessions(connection)
        except (OSError, SessionCommandError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "sessions": [
                {"session_id": str(info.session_id), "modified": info.modified.isoformat()}
                for info in found
            ]
        }

    @app.get("/audio/{session_id}/{name}")
    async def audio(session_id: UUID, name: str) -> FileResponse:
        """Serve one clip out of a session's audio folder.

        Read straight from disk rather than from the live sessions, so a clip
        keeps playing after a reload and old sessions stay listenable.
        """
        base = (session_dir(session_id, WEB_CLIENT_COMPONENT, create=False) / AUDIO_DIR).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="no such clip")
        return FileResponse(path)

    @app.post("/takes/{session_id}")
    async def take(session_id: UUID, request: Request) -> dict:
        """Accept one recorded transmission, transcribe it, and send it on."""
        radio = live.get(session_id)
        if radio is None:
            raise HTTPException(status_code=404, detail="no such session")

        recording = await request.body()
        if not recording:
            raise HTTPException(status_code=400, detail="empty recording")

        content_type = (request.headers.get("content-type") or "").split(";")[0].strip()
        suffix = SUFFIXES.get(content_type, "bin")
        message_id = uuid4()
        clip = await asyncio.to_thread(
            radio.transcript.save_audio, "player", message_id, recording, suffix
        )

        if voice is None:
            raise HTTPException(status_code=503, detail="voice input is unavailable")

        try:
            text = (
                await asyncio.to_thread(
                    transcribe, voice, recording, f"take.{suffix}", content_type or "audio/webm"
                )
            ).strip()
        except VoiceError as exc:
            logger.warning("Could not transcribe %s: %s", message_id, exc)
            raise HTTPException(
                status_code=502, detail="could not transcribe the recording"
            ) from exc

        if not text:
            return {"id": str(message_id), "text": "", "audio": clip}

        message = UserMessage(id=message_id, content=text)
        radio.transcript.log("user", message.id, text, clip)
        await radio.upstream.send(message.model_dump_json())
        await radio.browser.send_json(
            {"type": "user", "id": str(message.id), "text": text, "audio": clip}
        )
        return {"id": str(message_id), "text": text, "audio": clip}

    @app.websocket("/ws")
    async def channel(browser: WebSocket) -> None:
        """Hold one game open for one page, for as long as the tab is there."""
        await browser.accept()
        resume = browser.query_params.get("resume")

        try:
            upstream = await connect(upstream_uri)
        except OSError as exc:
            await browser.send_json({"type": "error", "text": f"cannot reach the game: {exc}"})
            await browser.close()
            return

        try:
            session_id = await open_session(upstream, UUID(resume) if resume else None)
        except (SessionCommandError, ValueError) as exc:
            await browser.send_json({"type": "error", "text": str(exc)})
            await upstream.close()
            await browser.close()
            return

        radio = Radio(
            session_id=session_id,
            upstream=upstream,
            transcript=Transcript(session_id, WEB_CLIENT_COMPONENT),
            browser=browser,
        )
        live[session_id] = radio
        radio.transcript.log("session", None, f"opened on {upstream_uri}")
        logger.info("Session %s open for a browser", session_id)

        await browser.send_json(
            {"type": "session", "session_id": str(session_id), "voice": voice is not None}
        )

        pump = asyncio.create_task(_pump(radio, streaming_voice))
        receiver = asyncio.create_task(browser.receive_text())
        try:
            while True:  # The page sends keep-alives; microphone takes use HTTP.
                done, _ = await asyncio.wait({pump, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if pump in done:
                    await pump  # Surface relay errors and close when the game ends.
                    break
                await receiver
                receiver = asyncio.create_task(browser.receive_text())
        except (WebSocketDisconnect, ConnectionClosed, RuntimeError):
            pass
        finally:
            pump.cancel()
            receiver.cancel()
            await asyncio.gather(pump, receiver, return_exceptions=True)
            if live.get(session_id) is radio:
                live.pop(session_id, None)
            with contextlib.suppress(ConnectionClosed):
                await upstream.close()
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await browser.close()
            logger.info("Session %s closed", session_id)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    host = args.host if args.host is not None else config.client.host
    port = args.port if args.port is not None else config.client.port

    app = create_app(f"ws://{host}:{port}", config.client.elevenlabs_api_key)
    print(f"Cabin Fever x86 (core version {__version__}) on http://{args.web_host}:{args.web_port}")
    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level="warning")


if __name__ == "__main__":
    main()
