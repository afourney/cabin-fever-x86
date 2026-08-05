"""Voice relay behavior of the optional Zello client."""

import json
from uuid import uuid4

import pytest

from cabin_fever_x86_core.messages import AssistantMessage
from cabin_fever_x86_core.zello_client._main import ZelloBridge, _static_burst


class Transcript:
    def __init__(self):
        self.audio = []
        self.records = []

    def save_audio(self, kind, message_id, data, suffix):
        self.audio.append((kind, message_id, data, suffix))
        return f"audio/{kind}_{message_id}.{suffix}"

    def log(self, speaker, message_id, text, audio=None):
        self.records.append((speaker, message_id, text, audio))


class Upstream:
    def __init__(self, incoming=()):
        self.incoming = list(incoming)
        self.sent = []

    def __aiter__(self):
        async def messages():
            for message in self.incoming:
                yield message

        return messages()

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class VoiceMessage:
    def __init__(self, sender, audio=b"OggS-player"):
        self.sender = sender
        self.audio = audio


class TextMessage:
    pass


class Zello:
    def __init__(self, incoming=()):
        self.incoming = incoming
        self.sent = []

    def messages(self):
        async def messages():
            for message in self.incoming:
                yield message

        return messages()

    async def send_voice(self, audio):
        self.sent.append(audio)


@pytest.mark.asyncio
async def test_only_authorized_voice_is_transcribed_and_forwarded(monkeypatch) -> None:
    zello = Zello([TextMessage(), VoiceMessage("mallory"), VoiceMessage("Alice")])
    upstream = Upstream()
    transcript = Transcript()
    monkeypatch.setattr(
        "cabin_fever_x86_core.zello_client._main.transcribe",
        lambda client, audio, filename, mimetype: "open the mailbox",
    )
    bridge = ZelloBridge(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await bridge._receive_zello()

    assert len(upstream.sent) == 1
    assert upstream.sent[0]["type"] == "user"
    assert upstream.sent[0]["content"] == "open the mailbox"
    assert transcript.audio[0][0::2] == ("player", b"OggS-player")
    assert transcript.records[0][0] == "user"


@pytest.mark.asyncio
async def test_assistant_text_is_synthesized_sent_and_logged(monkeypatch) -> None:
    message = AssistantMessage(id=uuid4(), content="Try opening it.")
    upstream = Upstream([message.model_dump_json()])
    zello = Zello()
    transcript = Transcript()
    generated = []

    def fake_synthesize(client, text, voice_id=None, output_format=None):
        generated.append((text, output_format))
        return b"OggS-clean"

    monkeypatch.setattr("cabin_fever_x86_core.zello_client._main.synthesize", fake_synthesize)
    bridge = ZelloBridge(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await bridge._receive_server()

    assert generated == [("Try opening it.", "opus_48000_64")]
    assert zello.sent == [b"OggS-clean"]
    assert transcript.audio[0][0::2] == ("clean", b"OggS-clean")
    assert transcript.records == [
        (
            "assistant",
            message.id,
            "Try opening it.",
            f"audio/clean_{message.id}.ogg",
        )
    ]


def test_static_burst_is_ogg_opus() -> None:
    assert _static_burst().startswith(b"OggS")


@pytest.mark.asyncio
async def test_empty_assistant_transmission_sends_static_without_synthesis(monkeypatch) -> None:
    message = AssistantMessage(id=uuid4(), content="")
    upstream = Upstream([message.model_dump_json()])
    zello = Zello()
    transcript = Transcript()
    monkeypatch.setattr(
        "cabin_fever_x86_core.zello_client._main.synthesize",
        lambda *_args, **_kwargs: pytest.fail("empty transmissions must not be synthesized"),
    )
    monkeypatch.setattr(
        "cabin_fever_x86_core.zello_client._main._static_burst",
        lambda: b"OggS-static",
    )
    bridge = ZelloBridge(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await bridge._receive_server()

    assert zello.sent == [b"OggS-static"]
    assert transcript.audio[0][0::2] == ("clean", b"OggS-static")
    assert transcript.records == [("assistant", message.id, "", f"audio/clean_{message.id}.ogg")]
