"""Pure behavior of the optional Telegram transport."""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cabin_fever_x86_core.telegram_client._main import TelegramBridge, is_stale, split_message


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
