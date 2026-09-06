"""Voice relay behavior of the optional Zello gateway."""

import asyncio
import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cabin_fever_x86_core.config import Config
from cabin_fever_x86_core.messages import AssistantMessage, ErrorResult, SessionResult
from cabin_fever_x86_core.zello_gateway import _main
from cabin_fever_x86_core.zello_gateway._main import ZelloGateway, _static_burst


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
        "cabin_fever_x86_core.zello_gateway._main.transcribe",
        lambda client, audio, filename, mimetype: "open the mailbox",
    )
    gateway = ZelloGateway(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await gateway._receive_zello()

    assert len(upstream.sent) == 1
    assert upstream.sent[0]["type"] == "user"
    assert upstream.sent[0]["content"] == "open the mailbox"
    assert transcript.audio[0][0::2] == ("player", b"OggS-player")
    assert transcript.records[0][0] == "user"


async def test_only_contributors_for_this_channel_are_processed(monkeypatch) -> None:
    config = Config.model_validate(
        {
            "users": [
                {
                    "user_id": "guest",
                    "identities": [
                        {"type": "guest"},
                        {"type": "zello", "channel": "game", "account_id": "Alice"},
                        {"type": "zello", "channel": "game", "account_id": "Bob"},
                        {"type": "zello", "channel": "other", "account_id": "Mallory"},
                    ],
                }
            ]
        }
    )
    user_id, contributors = config.zello_access("game")
    assert user_id == "guest"
    zello = Zello(
        [
            VoiceMessage("mallory", b"other-channel"),
            VoiceMessage("spectator", b"spectator"),
            TextMessage(),
            VoiceMessage("ALICE", b"alice"),
            VoiceMessage("bob", b"bob"),
        ]
    )
    upstream = Upstream()
    transcript = Transcript()
    transcribed = []

    def transcribe(client, audio, filename, mimetype):
        transcribed.append(audio)
        return audio.decode()

    monkeypatch.setattr(_main, "transcribe", transcribe)
    gateway = ZelloGateway(zello, VoiceMessage, upstream, transcript, object(), contributors)

    await gateway._receive_zello()

    assert transcribed == [b"alice", b"bob"]
    assert [clip[2] for clip in transcript.audio] == transcribed
    assert [message["content"] for message in upstream.sent] == ["alice", "bob"]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "client: {elevenlabs_api_key: test-key}\n"
        "zello: {credentials_file: keys.yaml}\n"
        "users:\n"
        "  - user_id: player\n"
        "    identities:\n"
        "      - {type: zello, channel: game, account_id: Alice}\n"
        "      - {type: zello, channel: game, account_id: Bob}\n"
        "      - {type: zello, channel: other, account_id: OtherSender}\n"
    )
    args = SimpleNamespace(config=str(path), host=None, port=None)
    monkeypatch.setattr(_main, "_parse_args", lambda: args)
    monkeypatch.setattr(_main, "_zello_api", lambda: (object(), object(), object()))
    return args, path


def test_startup_resolves_channel_owner_and_contributors(cli, monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(_main, "run_channels", run)

    _main.main()

    run.assert_awaited_once_with(
        "127.0.0.1",
        5000,
        "keys.yaml",
        {"game": ("player", {"alice", "bob"}), "other": ("player", {"othersender"})},
        "test-key",
    )


def test_cli_host_and_port_overrides(cli, monkeypatch) -> None:
    args, _ = cli
    args.host = "server.example"
    args.port = 1234
    run = AsyncMock()
    monkeypatch.setattr(_main, "run_channels", run)
    _main.main()
    assert run.await_args.args[:2] == ("server.example", 1234)


def test_unconfigured_channel_fails_before_upstream_or_paid_calls(cli, monkeypatch, capsys) -> None:
    _, path = cli
    path.write_text(
        "zello: {credentials_file: keys.yaml}\n"
        "users: [{user_id: guest, identities: [{type: guest}]}]\n"
    )
    monkeypatch.setattr(
        _main, "run_channels", AsyncMock(side_effect=AssertionError("must not run"))
    )

    with pytest.raises(SystemExit) as error:
        _main.main()

    assert error.value.code == 1
    assert "No Zello identities" in capsys.readouterr().err


def test_conflicting_channel_owners_fail_before_opening_a_game(cli, monkeypatch, capsys) -> None:
    _, path = cli
    with path.open("a") as handle:
        handle.write(
            "  - user_id: second-player\n"
            "    identities: [{type: zello, channel: game, account_id: Charlie}]\n"
        )
    monkeypatch.setattr(
        _main, "run_channels", AsyncMock(side_effect=AssertionError("must not run"))
    )

    with pytest.raises(SystemExit) as error:
        _main.main()

    assert error.value.code == 1
    assert "must belong to one user" in capsys.readouterr().err


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

    monkeypatch.setattr("cabin_fever_x86_core.zello_gateway._main.synthesize", fake_synthesize)
    gateway = ZelloGateway(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await gateway._receive_server()

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
        "cabin_fever_x86_core.zello_gateway._main.synthesize",
        lambda *_args, **_kwargs: pytest.fail("empty transmissions must not be synthesized"),
    )
    monkeypatch.setattr(
        "cabin_fever_x86_core.zello_gateway._main._static_burst",
        lambda: b"OggS-static",
    )
    gateway = ZelloGateway(zello, VoiceMessage, upstream, transcript, object(), {"alice"})

    await gateway._receive_server()

    assert zello.sent == [b"OggS-static"]
    assert transcript.audio[0][0::2] == ("clean", b"OggS-static")
    assert transcript.records == [("assistant", message.id, "", f"audio/clean_{message.id}.ogg")]


@pytest.mark.parametrize("option", ["--resume", "--list-sessions", "--continue"])
def test_removed_cli_options_are_rejected(option) -> None:
    with pytest.raises(SystemExit) as error:
        _main._parse_args([option])
    assert error.value.code == 2


def test_help_does_not_load_config_or_optional_dependencies(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["cf86-zello", "--help"])
    monkeypatch.setattr(_main, "_zello_api", lambda: pytest.fail("must not load zelpy"))
    monkeypatch.setattr(_main, "load_config", lambda _: pytest.fail("must not load config"))
    with pytest.raises(SystemExit) as error:
        _main.main()
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--config" in help_text
    assert "--resume" not in help_text
    assert "--list-sessions" not in help_text


def test_missing_optional_dependency_explains_install(monkeypatch) -> None:
    real_import = builtins.__import__

    def import_without_zelpy(name, *args, **kwargs):
        if name == "zelpy":
            raise ImportError("no zelpy")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_zelpy)
    with pytest.raises(_main.ZelloGatewayError, match=r"cabin-fever-x86-core\[zello\]"):
        _main._zello_api()


@pytest.mark.parametrize(
    "replacement, message",
    [
        ("", "client.elevenlabs_api_key is required"),
        ("client: {elevenlabs_api_key: test-key}\n", "required zello section"),
    ],
)
def test_missing_required_config(cli, capsys, replacement, message) -> None:
    _, path = cli
    if replacement:
        path.write_text(replacement)
    else:
        path.write_text(path.read_text().replace("client: {elevenlabs_api_key: test-key}\n", ""))
    with pytest.raises(SystemExit) as error:
        _main.main()
    assert error.value.code == 1
    assert message in capsys.readouterr().err


def test_cli_reports_nested_channel_failure(cli, monkeypatch, capsys) -> None:
    failure = ExceptionGroup(
        "channels", [_main.ZelloGatewayError("channel 'game': missing session")]
    )
    monkeypatch.setattr(_main, "run_channels", AsyncMock(side_effect=failure))
    with pytest.raises(SystemExit) as error:
        _main.main()
    assert error.value.code == 1
    assert "channel 'game': missing session" in capsys.readouterr().err


class LiveStream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.receiving = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed = False
        self.exit_error = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        if self.exit_error:
            raise self.exit_error

    def __aiter__(self):
        async def messages():
            self.receiving.set()
            try:
                while True:
                    message = await self.queue.get()
                    if message is None:
                        return
                    if isinstance(message, Exception):
                        raise message
                    yield message
            finally:
                self.finished.set()

        return messages()


class LiveUpstream(LiveStream):
    def __init__(self, owner, harness):
        super().__init__()
        self.owner = owner
        self.harness = harness
        self.sent = []
        self.user_message = asyncio.Event()
        self.session_id = None
        self.reply = None

    async def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["type"] == "user":
            self.user_message.set()
            return
        resume = message.get("session_id")
        self.harness.opens.append((self.owner, resume))
        # Yield so all channels' startup and persistence updates overlap.
        await asyncio.sleep(0)
        if resume and (self.owner, resume) not in self.harness.saved:
            self.reply = ErrorResult(message="saved session unavailable").model_dump_json()
            return
        self.session_id = resume or str(uuid4())
        if self.harness.wrong_resume and resume:
            self.session_id = str(uuid4())
        self.harness.saved.add((self.owner, self.session_id))
        self.reply = SessionResult(
            request_id=message["id"], session_id=self.session_id
        ).model_dump_json()

    async def recv(self):
        return self.reply


class LiveZello(LiveStream):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel
        self.sent = []
        self.replied = asyncio.Event()

    def messages(self):
        return self.__aiter__()

    async def send_voice(self, audio):
        self.sent.append(audio)
        self.replied.set()


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    harness = SimpleNamespace(
        connections=[],
        zellos={},
        opens=[],
        saved=set(),
        wrong_resume=False,
        transcripts={},
        transcribed=[],
        synthesized=[],
    )

    def connect(uri, *, additional_headers):
        assert uri == "ws://localhost:5001"
        assert set(additional_headers) == {"X-CF86-User-ID"}
        connection = LiveUpstream(additional_headers["X-CF86-User-ID"], harness)
        harness.connections.append(connection)
        return connection

    def zello(credentials, channel):
        assert credentials == "fake-credentials"
        connection = LiveZello(channel)
        harness.zellos[channel] = connection
        return connection

    real_transcript = _main.Transcript

    def transcript(session_id, component, *, user_id):
        result = real_transcript(session_id, component, user_id=user_id)
        harness.transcripts[(user_id, str(session_id))] = result
        return result

    def transcribe(client, audio, *args):
        harness.transcribed.append(audio)
        return audio.decode()

    def synthesize(client, text, **kwargs):
        harness.synthesized.append(text)
        return b"OggS-" + text.encode()

    monkeypatch.setattr(_main, "connect", connect)
    monkeypatch.setattr(_main, "_zello_api", lambda: (VoiceMessage, zello, object))
    monkeypatch.setattr(_main, "load_credentials", lambda *args: "fake-credentials")
    monkeypatch.setattr(_main, "ElevenLabs", lambda **kwargs: object())
    monkeypatch.setattr(_main, "Transcript", transcript)
    monkeypatch.setattr(_main, "transcribe", transcribe)
    monkeypatch.setattr(_main, "synthesize", synthesize)
    return harness


CHANNELS = {
    "one": ("alice", {"alice"}),
    "two": ("alice", {"bob"}),
    "three": ("bob", {"charlie"}),
}


async def wait_ready(service, count):
    async with asyncio.timeout(3):
        while len(service.zellos) < count:
            await asyncio.sleep(0)
        for connection in [*service.connections, *service.zellos.values()]:
            await connection.receiving.wait()


def start_service(channels=CHANNELS):
    return asyncio.create_task(
        _main.run_channels("localhost", 5001, "keys.yaml", channels, "test-key")
    )


async def cancel_service(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def assert_closed(service):
    for connection in [*service.connections, *service.zellos.values()]:
        assert connection.closed
        if connection.receiving.is_set():
            assert connection.finished.is_set()


async def test_concurrent_channels_persist_resume_and_isolate_traffic(service) -> None:
    task = start_service()
    try:
        await wait_ready(service, 3)
        assert not task.done()
        state = {owner: _main.ChannelSessions(owner).sessions for owner in ("alice", "bob")}
        assert set(state["alice"]) == {"one", "two"}
        assert set(state["bob"]) == {"three"}
        assert len({session for sessions in state.values() for session in sessions.values()}) == 3
        assert sorted(service.opens) == [("alice", None), ("alice", None), ("bob", None)]

        for channel, (owner, contributors) in CHANNELS.items():
            zello = service.zellos[channel]
            upstream = next(
                c for c in service.connections if c.session_id == str(state[owner][channel])
            )
            zello.queue.put_nowait(VoiceMessage("spectator", b"not-recorded"))
            # An identity allowed only on a different channel is still a spectator.
            other_sender = "bob" if channel == "one" else "alice"
            zello.queue.put_nowait(VoiceMessage(other_sender, b"wrong-channel"))
            zello.queue.put_nowait(TextMessage())
            zello.queue.put_nowait(VoiceMessage(next(iter(contributors)).upper(), channel.encode()))
            async with asyncio.timeout(3):
                await upstream.user_message.wait()
            assert upstream.sent[-1]["content"] == channel

            message = AssistantMessage(content=f"reply-{channel}")
            upstream.queue.put_nowait(message.model_dump_json())
            async with asyncio.timeout(3):
                await zello.replied.wait()
            # Wait until the assistant transcript append after send_voice completes.
            await asyncio.sleep(0)
            assert zello.sent == [f"OggS-reply-{channel}".encode()]
            transcript = service.transcripts[(owner, upstream.session_id)]
            assert transcript.dir == Path(
                f"data/users/{owner}/sessions/{upstream.session_id}/zello_gateway"
            )
            records = [json.loads(line) for line in transcript.path.read_text().splitlines()]
            assert [record["text"] for record in records[1:]] == [channel, f"reply-{channel}"]
            assert len(list(transcript.audio_dir.iterdir())) == 2
        assert service.transcribed == [b"one", b"two", b"three"]
    finally:
        await cancel_service(task)
    assert_closed(service)

    service.connections.clear()
    service.zellos.clear()
    service.opens.clear()
    task = start_service()
    try:
        await wait_ready(service, 3)
        assert sorted(service.opens) == sorted(
            (owner, str(session))
            for owner, sessions in state.items()
            for session in sessions.values()
        )
        assert {owner: _main.ChannelSessions(owner).sessions for owner in state} == state
    finally:
        await cancel_service(task)
    assert_closed(service)


@pytest.mark.parametrize("source", ["server", "zello"])
@pytest.mark.parametrize("failure", [None, RuntimeError("relay failed")])
async def test_channel_disconnect_or_failure_closes_every_channel(service, source, failure) -> None:
    task = start_service()
    try:
        await wait_ready(service, 3)
        connection = service.connections[0] if source == "server" else service.zellos["one"]
        connection.queue.put_nowait(failure)
        with pytest.raises(ExceptionGroup) as error:
            async with asyncio.timeout(3):
                await task
        message = _main._error_message(error.value)
        assert "Zello channel" in message
        assert ("relay failed" if failure else "connection closed") in message
    finally:
        if not task.done():
            await cancel_service(task)
    assert_closed(service)


async def test_cleanup_failure_is_reported_and_other_channels_close(service) -> None:
    task = start_service()
    try:
        await wait_ready(service, 3)
        service.zellos["one"].exit_error = RuntimeError("close failed")
        service.zellos["two"].queue.put_nowait(RuntimeError("receive failed"))
        with pytest.raises(ExceptionGroup) as error:
            async with asyncio.timeout(3):
                await task
        message = _main._error_message(error.value)
        assert "close failed" in message
        assert "receive failed" in message
    finally:
        if not task.done():
            await cancel_service(task)
    assert_closed(service)


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", "null", '{"one": "not-a-uuid"}', '{"one": 1}', '{" ": null}'],
)
async def test_invalid_state_fails_before_any_game_opens(service, raw) -> None:
    state = _main.ChannelSessions("bob")
    state.path.parent.mkdir(parents=True)
    state.path.write_text(raw)
    with pytest.raises(_main.ZelloGatewayError, match="could not read Zello session state"):
        await _main.run_channels("localhost", 5001, "keys.yaml", CHANNELS, "key")
    assert not service.connections
    assert state.path.read_text() == raw


async def test_unavailable_session_is_not_replaced(service) -> None:
    state = _main.ChannelSessions("alice")
    saved = uuid4()
    state.remember("one", saved)
    with pytest.raises(ExceptionGroup) as error:
        await _main.run_channels(
            "localhost", 5001, "keys.yaml", {"one": ("alice", {"alice"})}, "key"
        )
    assert "saved session unavailable" in _main._error_message(error.value)
    assert service.opens == [("alice", str(saved))]
    assert _main.ChannelSessions("alice").sessions == {"one": saved}
    assert_closed(service)


async def test_wrong_resumed_session_is_rejected(service) -> None:
    saved = uuid4()
    _main.ChannelSessions("alice").remember("one", saved)
    service.saved.add(("alice", str(saved)))
    service.wrong_resume = True
    with pytest.raises(ExceptionGroup) as error:
        await _main.run_channels(
            "localhost", 5001, "keys.yaml", {"one": ("alice", {"alice"})}, "key"
        )
    assert "instead of saved session" in _main._error_message(error.value)
    assert _main.ChannelSessions("alice").sessions == {"one": saved}
    assert_closed(service)


async def test_channel_association_is_scoped_to_owner(service) -> None:
    old = uuid4()
    _main.ChannelSessions("old-owner").remember("one", old)
    task = start_service({"one": ("new-owner", {"alice"})})
    try:
        await wait_ready(service, 1)
        assert service.opens == [("new-owner", None)]
        assert _main.ChannelSessions("old-owner").sessions == {"one": old}
        assert _main.ChannelSessions("new-owner").sessions["one"] != old
    finally:
        await cancel_service(task)
    assert_closed(service)


async def test_state_write_failure_closes_upstream_and_reports_error(service, monkeypatch) -> None:
    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(ExceptionGroup) as error:
        await _main.run_channels(
            "localhost", 5001, "keys.yaml", {"one": ("alice", {"alice"})}, "key"
        )
    assert "could not save Zello session state" in _main._error_message(error.value)
    assert "disk failure" in _main._error_message(error.value)
    assert not service.zellos
    assert_closed(service)


def test_failed_atomic_write_preserves_prior_state(service, monkeypatch) -> None:
    state = _main.ChannelSessions("alice")
    saved = uuid4()
    state.remember("one", saved)
    old = state.path.read_bytes()

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(_main.ZelloGatewayError, match="could not save"):
        state.remember("two", uuid4())
    assert state.path.read_bytes() == old
    assert state.sessions == {"one": saved}
    with pytest.raises(_main.ZelloGatewayError, match="unfinished session state write"):
        _main.ChannelSessions("alice")


@pytest.mark.parametrize(
    "problem", ["duplicate-channel", "shared-session", "unreadable", "partial"]
)
async def test_ambiguous_or_unreadable_state_is_explicit(service, problem) -> None:
    state = _main.ChannelSessions("alice")
    state.path.parent.mkdir(parents=True)
    saved = str(uuid4())
    if problem == "duplicate-channel":
        state.path.write_text(f'{{"one": "{saved}", "one": "{uuid4()}"}}')
    elif problem == "shared-session":
        state.path.write_text(json.dumps({"one": saved, "two": saved}))
    elif problem == "unreadable":
        state.path.mkdir()
    else:
        state.path.with_suffix(".tmp").write_text(json.dumps({"one": saved}))
    with pytest.raises(_main.ZelloGatewayError, match="could not read Zello session state"):
        await _main.run_channels("localhost", 5001, "keys.yaml", CHANNELS, "key")
    assert not service.connections


async def test_cancellation_during_startup_closes_all_open_connections(
    service, monkeypatch
) -> None:
    started = asyncio.Event()
    opening = 0

    async def blocked_open(*args):
        nonlocal opening
        opening += 1
        if opening == 3:
            started.set()
        await asyncio.Future()

    monkeypatch.setattr(_main, "open_session", blocked_open)
    task = start_service()
    try:
        async with asyncio.timeout(3):
            await started.wait()
        assert len(service.connections) == 3
        assert not service.zellos
    finally:
        await cancel_service(task)
    assert_closed(service)


async def test_both_pump_failures_are_reported(service) -> None:
    task = start_service({"one": ("alice", {"alice"})})
    try:
        await wait_ready(service, 1)
        service.connections[0].queue.put_nowait(RuntimeError("server receive failed"))
        service.zellos["one"].queue.put_nowait(RuntimeError("zello receive failed"))
        with pytest.raises(ExceptionGroup) as error:
            async with asyncio.timeout(3):
                await task
        message = _main._error_message(error.value)
        assert "server receive failed" in message
        assert "zello receive failed" in message
    finally:
        if not task.done():
            await cancel_service(task)
    assert_closed(service)


async def test_zero_channels_fails(service) -> None:
    with pytest.raises(_main.ZelloGatewayError, match="no channels configured"):
        await _main.run_channels("localhost", 5001, "keys.yaml", {}, "key")
    assert not service.connections
