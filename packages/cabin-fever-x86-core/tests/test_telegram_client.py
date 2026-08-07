"""Pure behavior of the optional Telegram transport."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cabin_fever_x86_core.messages import AssistantMessage, CompactionCompleted
from cabin_fever_x86_core.telegram_client._main import (
    TelegramBridge,
    is_stale,
    split_message,
)


def test_short_message_is_unchanged() -> None:
    assert split_message("hello", limit=10) == ["hello"]


def test_message_splits_at_a_natural_boundary() -> None:
    assert split_message("one two three", limit=8) == ["one two", "three"]


def test_message_hard_splits_a_long_word() -> None:
    assert split_message("abcdefgh", limit=3) == ["abc", "def", "gh"]


def test_only_old_messages_are_stale() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert is_stale(now - timedelta(seconds=181), now)
    assert not is_stale(now - timedelta(seconds=180), now)


@pytest.mark.asyncio
async def test_rejected_account_is_logged_with_discoverable_id(caplog) -> None:
    class Event:
        sender_id = 8675309
        chat_id = 8675309
        is_private = True
        message = SimpleNamespace(text="hello", date=datetime.now(timezone.utc))

        def __init__(self):
            self.responses = []

        async def get_sender(self):
            return SimpleNamespace(username="jenny")

        async def respond(self, text):
            self.responses.append(text)

    event = Event()
    bridge = TelegramBridge(SimpleNamespace(), "ws://localhost:5000", set())

    with caplog.at_level(logging.WARNING):
        await bridge.handle(event)

    assert event.responses == ["Not authorized."]
    assert "user_id=8675309" in caplog.text
    assert "username=@jenny" in caplog.text


@pytest.mark.asyncio
async def test_continue_resumes_the_most_recent_server_session(monkeypatch) -> None:
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    bridge = TelegramBridge(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309}
    )
    latest = uuid4()
    opened = []

    async def find_latest():
        return latest

    async def open_session(account_id, chat_id, resume):
        opened.append((account_id, chat_id, resume))
        return SimpleNamespace(session_id=resume)

    monkeypatch.setattr(bridge, "_latest", find_latest)
    monkeypatch.setattr(bridge, "open", open_session)

    await bridge._handle_text(8675309, 8675309, "/continue")

    assert opened == [(8675309, 8675309, latest)]
    assert sent == [(8675309, f"Resumed session {latest}.")]


@pytest.mark.asyncio
async def test_compact_requests_compaction_and_waits_for_completion() -> None:
    sent = []
    wire = []
    session_id = uuid4()
    holder = {}

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    class Connection:
        async def send(self, raw):
            command = json.loads(raw)
            wire.append(command)
            session = holder["session"]
            session.pending_compactions[UUID(command["id"])].set_result(
                CompactionCompleted(request_id=command["id"], session_id=session_id)
            )

    session = SimpleNamespace(
        lock=asyncio.Lock(),
        connection=Connection(),
        pending_compactions={},
    )
    holder["session"] = session
    bridge = TelegramBridge(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309}
    )
    bridge.sessions[8675309] = session

    await bridge._handle_text(8675309, 8675309, "/compact")

    assert wire[0]["type"] == "compact_session"
    assert session.pending_compactions == {}
    assert sent == [(8675309, "Compaction completed.")]


@pytest.mark.asyncio
async def test_compact_requires_an_open_game() -> None:
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    bridge = TelegramBridge(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309}
    )

    await bridge._handle_text(8675309, 8675309, "/compact")

    assert sent == [(8675309, "No game is open.")]


@pytest.mark.asyncio
async def test_voice_note_is_transcribed_and_forwarded_without_an_echo(monkeypatch) -> None:
    class Connection:
        def __init__(self):
            self.sent = []

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    class Transcript:
        def __init__(self):
            self.audio = []
            self.records = []

        def save_audio(self, kind, message_id, data, suffix):
            self.audio.append((kind, message_id, data, suffix))
            return f"audio/{kind}_{message_id}.{suffix}"

        def log(self, speaker, message_id, text, audio=None):
            self.records.append((speaker, message_id, text, audio))

    class Message:
        file = SimpleNamespace(size=4, duration=1, mime_type="audio/ogg")

        async def download_media(self, *, file):
            assert file is bytes
            return b"OggS"

    bot = SimpleNamespace(sent=[])

    async def send_message(chat_id, text):
        bot.sent.append((chat_id, text))

    bot.send_message = send_message
    connection = Connection()
    transcript = Transcript()
    session = SimpleNamespace(lock=asyncio.Lock(), connection=connection, transcript=transcript)
    bridge = TelegramBridge(bot, "ws://localhost:5000", {8675309}, voice=object())
    bridge.sessions[8675309] = session
    monkeypatch.setattr(
        "cabin_fever_x86_core.telegram_client._main.transcribe",
        lambda client, audio, filename, mimetype: "open the mailbox",
    )

    await bridge._handle_voice(8675309, 8675309, Message())

    assert bot.sent == []
    assert connection.sent[0]["type"] == "user"
    assert connection.sent[0]["content"] == "open the mailbox"
    assert transcript.audio[0][2:] == (b"OggS", "ogg")
    assert transcript.records[0][0] == "user"
    assert transcript.records[0][2] == "open the mailbox"


@pytest.mark.asyncio
async def test_first_reply_is_captioned_voice_then_text_follows_text(monkeypatch) -> None:
    class Bot:
        def __init__(self):
            self.actions = []

        async def send_message(self, chat_id, text):
            self.actions.append(("text", chat_id, text))

        async def send_file(self, chat_id, file, *, voice_note, caption, parse_mode):
            self.actions.append(
                ("voice", chat_id, file.name, file.read(), voice_note, caption, parse_mode)
            )

    class Transcript:
        def __init__(self):
            self.records = []

        def save_audio(self, kind, message_id, data, suffix):
            assert (kind, data, suffix) == ("clean", b"OggS-opus", "ogg")
            return f"audio/clean_{message_id}.ogg"

        def log(self, speaker, message_id, text, audio=None):
            self.records.append((speaker, message_id, text, audio))

    bot = Bot()
    transcript = Transcript()
    session = SimpleNamespace(
        chat_id=8675309,
        voice_lock=asyncio.Lock(),
        transcript=transcript,
        has_replied=False,
        last_user_was_voice=False,
    )
    bridge = TelegramBridge(
        bot,
        "ws://localhost:5000",
        {8675309},
        voice=object(),
    )
    generated = []

    def fake_synthesize(client, text, voice_id=None, output_format=None):
        generated.append((client, text, voice_id, output_format))
        return b"OggS-opus"

    monkeypatch.setattr("cabin_fever_x86_core.telegram_client._main.synthesize", fake_synthesize)
    message = AssistantMessage(content="There is a lamp here.")

    await bridge._deliver_assistant(session, message)
    second = AssistantMessage(content="It is made of brass.")
    await bridge._deliver_assistant(session, second)
    session.last_user_was_voice = True
    third = AssistantMessage(content="Yes, I heard you.")
    await bridge._deliver_assistant(session, third)

    assert bot.actions == [
        (
            "voice",
            8675309,
            f"reply-{message.id}.ogg",
            b"OggS-opus",
            True,
            "There is a lamp here.",
            None,
        ),
        ("text", 8675309, "It is made of brass."),
        (
            "voice",
            8675309,
            f"reply-{third.id}.ogg",
            b"OggS-opus",
            True,
            "Yes, I heard you.",
            None,
        ),
    ]
    assert generated[0][1:] == (
        "There is a lamp here.",
        None,
        "opus_48000_64",
    )
    assert transcript.records == [
        (
            "assistant",
            message.id,
            "There is a lamp here.",
            f"audio/clean_{message.id}.ogg",
        ),
        ("assistant", second.id, "It is made of brass.", None),
        (
            "assistant",
            third.id,
            "Yes, I heard you.",
            f"audio/clean_{third.id}.ogg",
        ),
    ]


@pytest.mark.asyncio
async def test_an_empty_assistant_transmission_is_static_without_voice(monkeypatch) -> None:
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    records = []
    session = SimpleNamespace(
        chat_id=8675309,
        voice_lock=asyncio.Lock(),
        transcript=SimpleNamespace(log=lambda *record: records.append(record)),
        has_replied=False,
        last_user_was_voice=False,
    )
    bridge = TelegramBridge(
        SimpleNamespace(send_message=send_message),
        "ws://localhost:5000",
        {8675309},
        voice=object(),
    )
    monkeypatch.setattr(
        "cabin_fever_x86_core.telegram_client._main.synthesize",
        lambda *_args, **_kwargs: pytest.fail("empty transmissions must not be synthesized"),
    )
    message = AssistantMessage(content="")

    await bridge._deliver_assistant(session, message)

    assert sent == [(8675309, "[static]")]
    assert records == [("assistant", message.id, "", None)]
