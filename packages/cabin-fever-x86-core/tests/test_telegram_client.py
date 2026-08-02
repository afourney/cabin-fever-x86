"""Pure behavior of the optional Telegram transport."""

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
