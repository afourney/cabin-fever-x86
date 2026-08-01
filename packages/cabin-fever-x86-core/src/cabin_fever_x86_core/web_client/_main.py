"""Entry point for the Cabin Fever x86 web client.

Serves a page that holds the radio, and relays between it and the game server.
The browser captures and plays audio; this process keeps the ElevenLabs key,
does the transcription and the speech, and writes the session's record.

    uv run cf86-web
    open http://127.0.0.1:8000

Audio never travels as JSON: a finished take is POSTed as a blob, and replies
are fetched from ``/audio/...`` once the page is told they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from elevenlabs.client import ElevenLabs
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
from cabin_fever_x86_core.voice import TTS_SUFFIX, VoiceError, synthesize, transcribe

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
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
    voice = ElevenLabs(api_key=api_key) if api_key else None
    if voice is None:
        logger.warning("No ElevenLabs key: the radio will be text-only in both directions.")

    live: dict[UUID, Radio] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Game server: %s", upstream_uri)
        yield
        for radio in list(live.values()):
            await radio.upstream.close()

    app = FastAPI(title="Cabin Fever x86", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

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
            raise HTTPException(status_code=503, detail="no ElevenLabs key; cannot transcribe")

        try:
            text = (
                await asyncio.to_thread(
                    transcribe, voice, recording, f"take.{suffix}", content_type or "audio/webm"
                )
            ).strip()
        except VoiceError as exc:
            logger.warning("Could not transcribe %s: %s", message_id, exc)
            raise HTTPException(status_code=502, detail=f"transcription failed: {exc}") from exc

        if not text:
            return {"id": str(message_id), "text": "", "audio": clip}

        message = UserMessage(id=message_id, content=text)
        radio.transcript.log("user", message.id, text, clip)
        await radio.upstream.send(message.model_dump_json())
        await radio.browser.send_json(
            {"type": "user", "id": str(message.id), "text": text, "audio": clip}
        )
        return {"id": str(message_id), "text": text, "audio": clip}

    async def _pump(radio: Radio) -> None:
        """Relay the companion's transmissions to the page, in its voice."""
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
                continue  # a late answer to a session command

            clip: str | None = None
            # One at a time: two replies speaking over each other is worse
            # than one arriving a beat late.
            async with radio.lock:
                if voice is not None:
                    try:
                        spoken = await asyncio.to_thread(synthesize, voice, message.content)
                        clip = await asyncio.to_thread(
                            radio.transcript.save_audio,
                            "clean",
                            message.id,
                            spoken,
                            TTS_SUFFIX,
                        )
                    except VoiceError as exc:
                        logger.warning("Could not speak %s: %s", message.id, exc)

            radio.transcript.log("assistant", message.id, message.content, clip)
            await radio.browser.send_json(
                {
                    "type": "assistant",
                    "id": str(message.id),
                    "text": message.content,
                    "audio": f"/audio/{radio.session_id}/{Path(clip).name}" if clip else None,
                }
            )

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

        pump = asyncio.create_task(_pump(radio))
        try:
            while True:  # the page only sends keep-alives; audio goes over HTTP
                await browser.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            pump.cancel()
            live.pop(session_id, None)
            with_error = None
            with contextlib.suppress(ConnectionClosed):  # it may already be gone
                await upstream.close()
            logger.info("Session %s closed (%s)", session_id, with_error or "browser left")

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
