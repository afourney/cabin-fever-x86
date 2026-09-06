"""Pure behavior of the optional Telegram gateway."""

import asyncio
import json
import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cabin_fever_x86_core.config import Config
from cabin_fever_x86_core.messages import AssistantMessage, CompactionCompleted
from cabin_fever_x86_core.sessions import GUEST_USER_ID
from cabin_fever_x86_core.telegram_gateway import _main
from cabin_fever_x86_core.telegram_gateway._main import (
    TelegramGateway,
    _load_state,
    _save_state,
    _state_path,
    is_stale,
    split_message,
)


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _event(account_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        sender_id=account_id,
        chat_id=account_id,
        is_private=True,
        get_sender=AsyncMock(return_value=SimpleNamespace(username="player")),
        message=SimpleNamespace(text=text, voice=None, date=datetime.now(timezone.utc)),
    )


@pytest.fixture
def gateway():
    bot = SimpleNamespace(action=lambda *_: nullcontext(), send_message=AsyncMock())
    return TelegramGateway(bot, "ws://localhost:5000", {123: "guest", 456: "guest"})


@pytest.mark.parametrize("following", ["second", "/quit"])
async def test_overlapping_messages_share_one_game(gateway, monkeypatch, following) -> None:
    opening = asyncio.Event()
    release = asyncio.Event()
    connections = []

    async def connect(_account_id):
        connection = SimpleNamespace(send=AsyncMock(), close=AsyncMock())
        connections.append(connection)
        return connection

    async def open_session(_connection, _resume):
        opening.set()
        await release.wait()
        return uuid4()

    async def pump(_session):
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway, "_connect", connect)
    monkeypatch.setattr(gateway, "_pump", pump)
    monkeypatch.setattr(_main, "open_session", open_session)
    tasks = [asyncio.create_task(gateway.handle(_event(123, "first")))]
    try:
        await asyncio.wait_for(opening.wait(), timeout=1)
        tasks.append(asyncio.create_task(gateway.handle(_event(123, following))))
        await asyncio.sleep(0)  # Let the second handler reach the pending game creation.
        assert len(connections) == 1
        assert not tasks[1].done()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        sent = [json.loads(call.args[0])["content"] for call in connections[0].send.await_args_list]
        assert sent == (["first", "second"] if following == "second" else ["first"])
        assert (123 in gateway.sessions) is (following != "/quit")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await gateway.close()
    connections[0].close.assert_awaited_once()


async def test_busy_account_does_not_block_another_account(gateway, monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    handled = []

    async def handle_text(account_id, _chat_id, _text):
        if account_id == 123:
            started.set()
            await release.wait()
        handled.append(account_id)

    monkeypatch.setattr(gateway, "_handle_text", handle_text)
    first = asyncio.create_task(gateway.handle(_event(123, "first")))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(gateway.handle(_event(456, "second")), timeout=1)
        assert handled == [456]  # Independent even when both accounts map to guest.
        release.set()
        await asyncio.wait_for(first, timeout=1)
        assert handled == [456, 123]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_waiting_message_proceeds_after_failure_or_cancellation(
    gateway, monkeypatch, cancel
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    handled = []

    async def handle_text(_account_id, _chat_id, text):
        if text == "first":
            started.set()
            await release.wait()
            raise ValueError("could not open game")
        handled.append(text)

    monkeypatch.setattr(gateway, "_handle_text", handle_text)
    tasks = [asyncio.create_task(gateway.handle(_event(123, "first")))]
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        tasks.append(asyncio.create_task(gateway.handle(_event(123, "second"))))
        await asyncio.sleep(0)
        assert handled == []
        if cancel:
            tasks[0].cancel()
        else:
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        assert handled == ["second"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
    gateway = TelegramGateway(
        SimpleNamespace(), "ws://localhost:5000", Config().telegram_accounts()
    )

    with caplog.at_level(logging.WARNING):
        await gateway.handle(event)

    assert event.responses == ["Not authorized."]
    assert "user_id=8675309" in caplog.text
    assert "username=@jenny" in caplog.text


@pytest.mark.asyncio
async def test_continue_resumes_the_most_recent_server_session(monkeypatch) -> None:
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    gateway = TelegramGateway(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309: "guest"}
    )
    latest = uuid4()
    opened = []

    async def find_latest(account_id):
        assert account_id == 8675309
        return latest

    async def open_session(account_id, chat_id, resume):
        opened.append((account_id, chat_id, resume))
        return SimpleNamespace(session_id=resume)

    monkeypatch.setattr(gateway, "_latest", find_latest)
    monkeypatch.setattr(gateway, "open", open_session)

    await gateway._handle_text(8675309, 8675309, "/continue")

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
    gateway = TelegramGateway(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309: "guest"}
    )
    gateway.sessions[8675309] = session

    await gateway._handle_text(8675309, 8675309, "/compact")

    assert wire[0]["type"] == "compact_session"
    assert session.pending_compactions == {}
    assert sent == [(8675309, "Compaction completed.")]


@pytest.mark.asyncio
async def test_compact_requires_an_open_game() -> None:
    sent = []

    async def send_message(chat_id, text):
        sent.append((chat_id, text))

    gateway = TelegramGateway(
        SimpleNamespace(send_message=send_message), "ws://localhost:5000", {8675309: "guest"}
    )

    await gateway._handle_text(8675309, 8675309, "/compact")

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
    gateway = TelegramGateway(bot, "ws://localhost:5000", {8675309: "guest"}, voice=object())
    gateway.sessions[8675309] = session
    monkeypatch.setattr(
        "cabin_fever_x86_core.telegram_gateway._main.transcribe",
        lambda client, audio, filename, mimetype: "open the mailbox",
    )

    await gateway._handle_voice(8675309, 8675309, Message())

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
    gateway = TelegramGateway(
        bot,
        "ws://localhost:5000",
        {8675309: "guest"},
        voice=object(),
    )
    generated = []

    def fake_synthesize(client, text, voice_id=None, output_format=None):
        generated.append((client, text, voice_id, output_format))
        return b"OggS-opus"

    monkeypatch.setattr("cabin_fever_x86_core.telegram_gateway._main.synthesize", fake_synthesize)
    message = AssistantMessage(content="There is a lamp here.")

    await gateway._deliver_assistant(session, message)
    second = AssistantMessage(content="It is made of brass.")
    await gateway._deliver_assistant(session, second)
    session.last_user_was_voice = True
    third = AssistantMessage(content="Yes, I heard you.")
    await gateway._deliver_assistant(session, third)

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
    gateway = TelegramGateway(
        SimpleNamespace(send_message=send_message),
        "ws://localhost:5000",
        {8675309: "guest"},
        voice=object(),
    )
    monkeypatch.setattr(
        "cabin_fever_x86_core.telegram_gateway._main.synthesize",
        lambda *_args, **_kwargs: pytest.fail("empty transmissions must not be synthesized"),
    )
    message = AssistantMessage(content="")

    await gateway._deliver_assistant(session, message)

    assert sent == [(8675309, "[static]")]
    assert records == [("assistant", message.id, "", None)]


@pytest.mark.parametrize("user_id", ["guest", "alice"])
def test_session_state_lives_under_its_user(user_id) -> None:
    assert Path(_state_path(user_id)).parts == (
        "data",
        "users",
        user_id,
        "telegram_gateway",
        "sessions.json",
    )


def test_session_state_survives_a_round_trip(tmp_path) -> None:
    path = tmp_path / "users" / GUEST_USER_ID / "telegram_gateway" / "sessions.json"
    state = {8675309: uuid4()}

    _save_state(state, path)

    assert _load_state(path) == state


def test_reassigning_an_account_does_not_load_the_old_users_state() -> None:
    session_id = uuid4()
    _save_state({123: session_id}, _state_path("guest"))

    guest = TelegramGateway(SimpleNamespace(), "ws://localhost:5000", {123: "guest"})
    reassigned = TelegramGateway(SimpleNamespace(), "ws://localhost:5000", {123: "alice"})

    assert guest.last_sessions == {"guest": {123: session_id}}
    assert reassigned.last_sessions == {"alice": {}}


def test_unlisted_account_cannot_open_an_upstream_connection(monkeypatch) -> None:
    monkeypatch.setattr(_main, "connect", lambda *_args, **_kwargs: pytest.fail("must not connect"))
    gateway = TelegramGateway(SimpleNamespace(), "ws://localhost:5000", {123: "guest"})

    with pytest.raises(ValueError, match="not authorized"):
        gateway._connect(456)


def test_startup_passes_configured_identity_mapping_to_bot(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "telegram_gateway: {api_id: 123, api_hash: hash, bot_token: token}\n"
        "users:\n"
        "  - user_id: alice\n"
        "    identities: [{type: telegram, account_id: '111'}]\n"
        "  - user_id: guest\n"
        "    identities: [{type: guest}, {type: telegram, account_id: '222'}]\n"
    )
    monkeypatch.setattr(_main, "_telegram_api", lambda: (object(), object()))
    monkeypatch.setattr(
        _main, "_parse_args", lambda: SimpleNamespace(config=str(path), host=None, port=None)
    )
    run_bot = AsyncMock()
    monkeypatch.setattr(_main, "run_bot", run_bot)

    _main.main()

    run_bot.assert_awaited_once_with(
        123, "hash", "token", "ws://127.0.0.1:5000", {111: "alice", 222: "guest"}, None
    )


async def test_unlisted_voice_is_rejected_before_download_or_paid_calls(monkeypatch) -> None:
    event = SimpleNamespace(
        sender_id=456,
        chat_id=456,
        is_private=True,
        get_sender=AsyncMock(return_value=SimpleNamespace(username="unlisted")),
        respond=AsyncMock(),
        message=SimpleNamespace(voice=object(), download_media=AsyncMock()),
    )
    monkeypatch.setattr(
        _main, "transcribe", lambda *_args, **_kwargs: pytest.fail("must not transcribe")
    )
    gateway = TelegramGateway(SimpleNamespace(), "ws://localhost:5000", {123: "guest"})

    await gateway.handle(event)

    event.respond.assert_awaited_once_with("Not authorized.")
    event.message.download_media.assert_not_awaited()
    assert not gateway.sessions
