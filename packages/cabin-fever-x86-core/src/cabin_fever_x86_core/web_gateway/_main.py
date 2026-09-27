"""Entry point for the Cabin Fever x86 web gateway.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import httpx
import uvicorn
from elevenlabs.client import AsyncElevenLabs, ElevenLabs
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field, ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

from cabin_fever_x86_core import __version__
from cabin_fever_x86_core.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from cabin_fever_x86_core.messages import (
    RESUME_REQUIRED,
    SERVER_MESSAGE_ADAPTER,
    SESSION_REPLACED,
    AssistantMessage,
    ErrorResult,
    UserMessage,
)
from cabin_fever_x86_core.session_client import (
    SessionCommandError,
    list_sessions,
    open_owned_session,
)
from cabin_fever_x86_core.sessions import WEB_GATEWAY_COMPONENT, session_dir
from cabin_fever_x86_core.transcripts import AUDIO_DIR, Transcript
from cabin_fever_x86_core.voice import PCM_SAMPLE_RATE, VoiceError, stream_speech, transcribe
from cabin_fever_x86_core.web_gateway.auth import BrowserAuth

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
    user_id: str = "guest"
    auth_session_id: str | None = None
    owner_token: str | None = field(default=None, repr=False)
    connection_id: str = field(default_factory=lambda: str(uuid4()))
    active: bool = True
    retire: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class BrowserOpen(BaseModel):
    """Session request sent in the socket body, keeping recovery tokens out of URLs."""

    type: Literal["open"]
    resume: UUID | None = None
    mode: Literal["takeover", "recover"] = "takeover"
    owner_token: str | None = Field(default=None, repr=False, max_length=256)


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
    parser = argparse.ArgumentParser(description="Serve the Cabin Fever x86 web gateway.")
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


def create_app(upstream_uri: str, api_key: str | None, config: Config | None = None) -> FastAPI:
    """Build the web app. *upstream_uri* is the game server's websocket."""
    voice: ElevenLabs | None = None
    streaming_voice: AsyncElevenLabs | None = None
    if not api_key:
        logger.warning("No ElevenLabs key: the radio will be text-only in both directions.")

    auth = BrowserAuth(config if config is not None else Config())
    live: dict[tuple[str, UUID], Radio] = {}

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
    app.state.browser_auth = auth
    auth.install(app)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/pcm-player.js")
    async def pcm_player() -> FileResponse:
        """Serve the provider-independent streaming audio player."""
        return FileResponse(STATIC_DIR / "pcm-player.js", media_type="text/javascript")

    @app.get("/browser-auth.js")
    async def browser_auth_script() -> FileResponse:
        """Serve the browser's sign-in controller."""
        return FileResponse(STATIC_DIR / "browser-auth.js", media_type="text/javascript")

    @app.get("/session-picker.js")
    async def session_picker_script() -> FileResponse:
        """Serve the browser's new/resumed session picker."""
        return FileResponse(STATIC_DIR / "session-picker.js", media_type="text/javascript")

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
    async def sessions(request: Request) -> dict:
        """List what the game server has on file, for the resume list."""
        principal = auth.require(request)
        try:
            async with connect(
                upstream_uri, additional_headers={"X-CF86-User-ID": principal.user_id}
            ) as connection:
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
    async def audio(session_id: UUID, name: str, request: Request) -> FileResponse:
        """Serve one clip out of a session's audio folder.

        Read straight from disk rather than from the live sessions, so a clip
        keeps playing after a reload and old sessions stay listenable.
        """
        principal = auth.require(request)
        base = (
            session_dir(session_id, WEB_GATEWAY_COMPONENT, create=False, user_id=principal.user_id)
            / AUDIO_DIR
        ).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="no such clip")
        return FileResponse(path)

    @app.post("/takes/{session_id}")
    async def take(session_id: UUID, request: Request) -> dict:
        """Accept one recorded transmission, transcribe it, and send it on."""
        principal = auth.require(request)
        radio = live.get((principal.user_id, session_id))
        if radio is None or radio.auth_session_id != principal.sid:
            raise HTTPException(status_code=404, detail="no such session")

        def require_owner() -> None:
            if (
                not radio.active
                or live.get((principal.user_id, session_id)) is not radio
                or radio.upstream.state is not State.OPEN
                or request.headers.get("X-CF86-Owner-Token") != radio.owner_token
                or request.headers.get("X-CF86-Connection") != radio.connection_id
            ):
                raise HTTPException(
                    status_code=409, detail="Session moved. Explicitly resume to transmit."
                )

        require_owner()
        recording = await request.body()
        if not auth.valid(principal):
            raise HTTPException(status_code=401, detail="Sign in to use the radio")
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

        if not auth.valid(principal):
            raise HTTPException(status_code=401, detail="Sign in to use the radio")
        require_owner()
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
        try:
            auth.check_origin(browser)
            principal = auth.require(browser)
        except HTTPException:
            await browser.close(code=4401)
            return
        await browser.accept()
        upstream = None
        radio = None
        tasks = set()
        close_code = 1000
        try:
            try:
                resume = browser.query_params.get("resume")
                resume = UUID(resume) if resume else None
                mode, token = "legacy", None
                if browser.query_params.get("protocol") == "2":
                    async with asyncio.timeout(30):
                        request = BrowserOpen.model_validate(await browser.receive_json())
                    resume, mode, token = request.resume, request.mode, request.owner_token
            except (ValueError, TimeoutError) as exc:
                raise SessionCommandError(
                    "Invalid session request. Please resume again.", "resume_required"
                ) from exc
            if mode == "recover" and resume is None:
                raise SessionCommandError("Explicitly resume the session.", "resume_required")
            if not auth.valid(principal):
                close_code = 4401
                return
            upstream = await connect(
                upstream_uri, additional_headers={"X-CF86-User-ID": principal.user_id}
            )
            result = await open_owned_session(upstream, resume, mode=mode, owner_token=token)
            session_id = result.session_id
            key = (principal.user_id, session_id)
            previous = live.get(key)
            if previous is not None:
                previous.active = False
                previous.retire.set()
                await previous.finished.wait()
            if not auth.valid(principal):
                close_code = 4401
                return
            if upstream.state is not State.OPEN:
                close_code = SESSION_REPLACED if upstream.close_code == SESSION_REPLACED else 1011
                return
            radio = Radio(
                session_id=session_id,
                upstream=upstream,
                transcript=Transcript(session_id, WEB_GATEWAY_COMPONENT, user_id=principal.user_id),
                browser=browser,
                user_id=principal.user_id,
                auth_session_id=principal.sid,
                owner_token=result.owner_token,
            )
            live[key] = radio
            radio.transcript.log("session", None, f"opened on {upstream_uri}")
            logger.info("Session %s open for a browser", session_id)

            async def watch_authorization() -> None:
                while auth.valid(principal):
                    await asyncio.sleep(5)

            pump = asyncio.create_task(_pump(radio, streaming_voice))
            receiver = asyncio.create_task(browser.receive_text())
            guard = asyncio.create_task(watch_authorization())
            retired = asyncio.create_task(radio.retire.wait())
            closed = asyncio.create_task(upstream.wait_closed())
            tasks = {pump, receiver, guard, retired, closed}
            await browser.send_json(
                {
                    "type": "session",
                    "session_id": str(session_id),
                    "voice": voice is not None,
                    "owner_token": result.owner_token,
                    "connection_id": radio.connection_id,
                }
            )
            while True:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                if guard in done:
                    close_code = 4401
                    break
                if retired in done:
                    close_code = SESSION_REPLACED
                    break
                if closed in done:
                    close_code = (
                        SESSION_REPLACED if upstream.close_code == SESSION_REPLACED else 1011
                    )
                    break
                if pump in done:
                    await pump
                    break
                if await receiver == ".":
                    await browser.send_json({"type": "pong"})
                tasks.remove(receiver)
                receiver = asyncio.create_task(browser.receive_text())
                tasks.add(receiver)
        except SessionCommandError as exc:
            if exc.code in {"resume_required", "session_in_use", "cleanup_failed"}:
                close_code = RESUME_REQUIRED
            await browser.send_json({"type": "error", "text": str(exc), "code": exc.code})
        except OSError:
            # Includes upstream handshake timeouts, which don't revoke ownership.
            close_code = 1011
            await browser.send_json(
                {"type": "error", "text": "Cannot reach the game. Please try again."}
            )
        except ConnectionClosed as exc:
            close_code = exc.rcvd.code if exc.rcvd else 1011
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if radio is not None:
                radio.active = False
                if live.get(key) is radio:
                    live.pop(key, None)
            try:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if upstream is not None:
                    with contextlib.suppress(ConnectionClosed):
                        await upstream.close()
                with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                    await browser.close(code=close_code)
            finally:
                if radio is not None:
                    radio.finished.set()

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

    app = create_app(f"ws://{host}:{port}", config.client.elevenlabs_api_key, config)
    url = config.web_gateway.public_origin or f"http://{args.web_host}:{args.web_port}"
    print(f"Cabin Fever x86 (core version {__version__}) on {url}")
    uvicorn.run(app, host=args.web_host, port=args.web_port, log_level="warning")


if __name__ == "__main__":
    main()
