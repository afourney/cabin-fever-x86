"""Streaming voice transport and recording lifecycle, without provider calls."""

import asyncio
import json
import wave
from contextlib import aclosing
from uuid import uuid4

import httpx
import pytest
from elevenlabs.client import AsyncElevenLabs
from fastapi.testclient import TestClient

from cabin_fever_x86_core import transcripts
from cabin_fever_x86_core.messages import AssistantMessage
from cabin_fever_x86_core.voice import PCM_SAMPLE_RATE, VoiceError, stream_speech
from cabin_fever_x86_core.web_client import _main as web


class Browser:
    def __init__(self):
        self.messages = []
        self.first_audio = asyncio.Event()

    async def send_json(self, message):
        self.messages.append(message)

    async def send_bytes(self, packet):
        self.messages.append(packet)
        self.first_audio.set()


class Upstream:
    def __init__(self, *messages):
        self.messages = messages

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for message in self.messages:
            yield message.model_dump_json()


@pytest.fixture
def radio(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "session_dir", lambda *_: tmp_path)
    session_id = uuid4()
    return web.Radio(session_id, Upstream(), transcripts.Transcript(session_id, "web"), Browser())


async def test_first_audio_arrives_before_synthesis_finishes_and_wav_matches(radio, monkeypatch):
    release = asyncio.Event()
    message = AssistantMessage(content="Try the door.")
    radio.upstream = Upstream(message)

    async def audio(*_):
        yield b"\x01"  # A provider chunk may split a 16-bit sample.
        yield b"\x02\x03"
        await release.wait()
        yield b"\x04"

    monkeypatch.setattr(web, "stream_speech", audio)
    task = asyncio.create_task(web._pump(radio, object()))
    try:
        await asyncio.wait_for(radio.browser.first_audio.wait(), 1)
        assert not task.done()
        assert radio.browser.messages[0] == {
            "type": "assistant",
            "id": str(message.id),
            "text": message.content,
        }
        assert radio.browser.messages[1] == {
            "type": "audio_start",
            "id": str(message.id),
            "stream_id": 1,
            "format": "pcm_s16le",
            "sample_rate": PCM_SAMPLE_RATE,
            "channels": 1,
        }
        assert radio.browser.messages[2] == b"\0\0\0\1\x01\x02"
        assert not list(radio.transcript.audio_dir.glob("*.wav"))
    finally:
        release.set()
        await task

    assert radio.browser.messages[-2] == b"\0\0\0\1\x03\x04"
    assert radio.browser.messages[-1] == {"type": "audio_end", "stream_id": 1, "status": "complete"}
    record = json.loads(radio.transcript.path.read_text())
    with wave.open(str(radio.transcript.dir / record["audio"]), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 24000)
        assert wav.readframes(wav.getnframes()) == b"\x01\x02\x03\x04"
    assert not list(radio.transcript.audio_dir.glob("*.part"))


@pytest.mark.parametrize("failure", ["before", "after", "empty", "odd"])
async def test_failed_audio_ends_stream_without_publishing_partial_recording(
    radio, monkeypatch, failure
):
    async def audio(*_):
        if failure == "after":
            yield b"\x01\x02"
        if failure == "odd":
            yield b"\x01"
        if failure in {"before", "after"}:
            raise VoiceError("ElevenLabs private diagnostic")

    monkeypatch.setattr(web, "stream_speech", audio)
    radio.upstream = Upstream(AssistantMessage(content="Hello."))
    await web._pump(radio, object())
    assert radio.browser.messages[-1] == {"type": "audio_end", "stream_id": 1, "status": "error"}
    errors = [m for m in radio.browser.messages if isinstance(m, dict) and m["type"] == "error"]
    assert len(errors) == 1
    assert "ElevenLabs" not in errors[0]["text"]
    assert list(radio.transcript.audio_dir.iterdir()) == []
    assert json.loads(radio.transcript.path.read_text())["audio"] is None


async def test_cancellation_closes_provider_and_removes_partial_wav(radio, monkeypatch):
    closed = asyncio.Event()

    async def audio(*_):
        try:
            yield b"\0\0"
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(web, "stream_speech", audio)
    radio.upstream = Upstream(AssistantMessage(content="Hello."))
    task = asyncio.create_task(web._pump(radio, object()))
    await asyncio.wait_for(radio.browser.first_audio.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert list(radio.transcript.audio_dir.iterdir()) == []
    assert json.loads(radio.transcript.path.read_text())["text"] == "Hello."


async def test_replies_have_distinct_stream_ids_and_empty_reply_has_no_audio(radio, monkeypatch):
    async def audio(*_):
        yield b"\0\0"

    monkeypatch.setattr(web, "stream_speech", audio)
    radio.upstream = Upstream(*(AssistantMessage(content=text) for text in ["one", "", "two"]))
    await web._pump(radio, object())
    messages = radio.browser.messages
    assert [m["type"] for m in messages if isinstance(m, dict)] == [
        "assistant",
        "audio_start",
        "audio_end",
        "assistant",
        "assistant",
        "audio_start",
        "audio_end",
    ]
    assert [m[:4] for m in messages if isinstance(m, bytes)] == [b"\0\0\0\1", b"\0\0\0\2"]


async def test_no_voice_keeps_text(radio):
    radio.upstream = Upstream(AssistantMessage(content="Hello."))
    await web._pump(radio, None)
    assert len(radio.browser.messages) == 1
    assert radio.browser.messages[0]["text"] == "Hello."


async def test_provider_adapter_uses_stream_endpoint_and_closes_http_response():
    class Audio(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"\0\0" * 1024
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    audio = Audio()

    async def handle(request):
        assert request.url.path.endswith("/stream")
        assert request.url.params["output_format"] == "pcm_24000"
        assert json.loads(request.content)["text"] == "hello"
        return httpx.Response(200, stream=audio)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncElevenLabs(api_key="test", httpx_client=http)
        async with aclosing(stream_speech(client, "hello")) as stream:
            assert await anext(stream) == b"\0\0" * 1024
        assert audio.closed


def test_websocket_serves_pcm_and_saved_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(transcripts, "session_dir", lambda *_: tmp_path)
    monkeypatch.setattr(web, "session_dir", lambda *_, **__: tmp_path)
    message = AssistantMessage(content="Hello.")
    closed = []

    class Connection(Upstream):
        async def iterate(self):
            yield message.model_dump_json()
            await asyncio.Event().wait()

        async def close(self):
            closed.append(True)

    async def connect(_):
        return Connection()

    async def open_session(*_):
        return uuid4()

    async def audio(*_):
        yield b"\x01\x02"

    monkeypatch.setattr(web, "connect", connect)
    monkeypatch.setattr(web, "open_session", open_session)
    monkeypatch.setattr(web, "stream_speech", audio)
    with TestClient(web.create_app("ws://game", "test")) as client:
        assert client.get("/pcm-player.js").status_code == 200
        assert "ElevenLabs" not in client.get("/").text
        with client.websocket_connect("/ws") as browser:
            session = browser.receive_json()
            assert session["voice"] is True
            assert browser.receive_json()["type"] == "assistant"
            assert browser.receive_json()["type"] == "audio_start"
            assert browser.receive_bytes() == b"\0\0\0\1\x01\x02"
            assert browser.receive_json()["status"] == "complete"
            clip = client.get(f"/audio/{session['session_id']}/clean_{message.id}.wav")
            assert clip.status_code == 200
            assert clip.content.startswith(b"RIFF")
    assert closed
