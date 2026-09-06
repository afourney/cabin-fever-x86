"""A first test, mostly to prove the harness runs.

Config loading is a reasonable thing to pin down first: every entry point goes
through it, and it is pure enough to test without a server, a model, or a
sound card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cabin_fever_x86_core.config import ConfigError, GuestIdentity, load_config


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_defaults_when_the_file_says_nothing(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "{}\n"))
    assert config.client.host == "127.0.0.1"
    assert config.server.port == 5000
    assert config.server.ai_client.provider == "openai"
    assert config.server.cabin_events.inactivity_timeout == 300
    assert config.launcher.package_locator is None
    assert config.telegram_gateway.bot_token is None
    assert config.telegram_accounts() == {}
    assert config.users[0].user_id == "guest"
    assert config.users[0].identities == [GuestIdentity(type="guest")]


def test_cabin_event_inactivity_timeout_is_configurable(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "server:\n  cabin_events:\n    inactivity_timeout: 60\n"))
    assert config.server.cabin_events.inactivity_timeout == 60


def test_launcher_package_locator_is_optional(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "launcher:\n  package_locator: ./dist/core-package.whl\n"))
    assert config.launcher.package_locator == "./dist/core-package.whl"


def test_env_vars_are_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF86_TEST_KEY", "sk-from-the-environment")
    config = load_config(write(tmp_path, "client:\n  elevenlabs_api_key: ${CF86_TEST_KEY}\n"))
    assert config.client.elevenlabs_api_key == "sk-from-the-environment"


def test_telegram_config_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CF86_TELEGRAM_TOKEN", "123:secret")
    config = load_config(
        write(
            tmp_path,
            """telegram_gateway:
  bot_token: ${CF86_TELEGRAM_TOKEN}
  api_id: 12345
  api_hash: hash
users:
  - user_id: guest
    identities:
      - type: guest
      - type: telegram
        account_id: "111"
  - user_id: alice
    identities:
      - type: telegram
        account_id: 222
      - type: telegram
        account_id: "333"
""",
        )
    )

    assert config.telegram_gateway.bot_token == "123:secret"
    assert config.telegram_gateway.api_id == 12345
    assert config.telegram_gateway.api_hash == "hash"
    assert config.telegram_accounts() == {111: "guest", 222: "alice", 333: "alice"}


@pytest.mark.parametrize(
    "body",
    [
        "users: []",
        "users: [{user_id: guest, identities: [{type: guest}]}]",
        "users: [{user_id: alice, identities: []}]",
    ],
)
def test_no_telegram_identities_means_no_telegram_access(tmp_path: Path, body: str) -> None:
    assert load_config(write(tmp_path, body)).telegram_accounts() == {}


@pytest.mark.parametrize(
    "users",
    [
        '[{user_id: "../alice", identities: []}]',
        "[{user_id: Alice, identities: []}]",
        "[{user_id: guest, identities: []}, {user_id: guest, identities: []}]",
        "[{user_id: alice, identities: [{type: guest}]}]",
        "[{user_id: guest, identities: [{type: guest}, {type: guest}]}]",
        "[{user_id: guest, identities: [{type: guest, account_id: 1}]}]",
        "[{user_id: alice, identities: [{type: telegram}]}]",
        "[{user_id: alice, identities: [{type: telegram, account_id: 0}]}]",
        "[{user_id: alice, identities: [{type: telegram, account_id: -1}]}]",
        "[{user_id: alice, identities: [{type: telegram, account_id: true}]}]",
        "[{user_id: alice, identities: [{type: telegram, account_id: 1.5}]}]",
        '[{user_id: alice, identities: [{type: telegram, account_id: "alice"}]}]',
        '[{user_id: alice, identities: [{type: telegram, account_id: "\uff11\uff12\uff13"}]}]',
        "[{user_id: alice, identities: [{type: github, account_id: 1}]}]",
        (
            "[{user_id: alice, identities: [{type: telegram, account_id: 1}, "
            '{type: telegram, account_id: "01"}]}]'
        ),
        (
            "[{user_id: alice, identities: [{type: telegram, account_id: 1}]}, "
            '{user_id: bob, identities: [{type: telegram, account_id: "1"}]}]'
        ),
    ],
)
def test_invalid_users_are_rejected(tmp_path: Path, users: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"users: {users}\n"))


def test_old_telegram_allowlist_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allowed_accounts"):
        load_config(write(tmp_path, "telegram_gateway: {allowed_accounts: [111]}\n"))


def test_zello_config_is_loaded(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            """zello:
  credentials_file: ~/.apikeys/zello.yaml
users:
  - user_id: player
    identities:
      - type: zello
        channel: Cabin Fever x86
        account_id: Alice
      - type: zello
        channel: Cabin Fever x86
        account_id: Bob
  - user_id: guest
    identities:
      - type: guest
      - type: zello
        channel: Other channel
        account_id: Alice
""",
        )
    )

    assert config.zello is not None
    assert config.zello.credentials_file == "~/.apikeys/zello.yaml"
    assert config.zello_channels() == {
        "Cabin Fever x86": ("player", {"alice", "bob"}),
        "Other channel": ("guest", {"alice"}),
    }
    assert config.zello_access("Cabin Fever x86") == ("player", {"alice", "bob"})
    assert config.zello_access("Other channel") == ("guest", {"alice"})
    assert config.telegram_accounts() == {}


@pytest.mark.parametrize("body", ["zello: {}\n", "zello: {channel: channel}\n"])
def test_zello_section_requires_credentials(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body))


@pytest.mark.parametrize(
    "identities",
    [
        "[{type: zello, account_id: alice}]",
        "[{type: zello, channel: channel}]",
        "[{type: zello, channel: ' ', account_id: alice}]",
        "[{type: zello, channel: channel, account_id: ' '}]",
        "[{type: zello, channel: channel, account_id: 123}]",
        (
            "[{type: zello, channel: channel, account_id: Alice}, "
            "{type: zello, channel: channel, account_id: alice}]"
        ),
    ],
)
def test_invalid_zello_identities_are_rejected(tmp_path: Path, identities: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, f"users: [{{user_id: guest, identities: {identities}}}]"))


@pytest.mark.parametrize("other_sender", ["ALICE", "bob"])
def test_a_zello_channel_cannot_have_multiple_owners(tmp_path: Path, other_sender: str) -> None:
    with pytest.raises(ConfigError, match="must belong to one user"):
        load_config(
            write(
                tmp_path,
                "users:\n"
                "  - user_id: alice\n"
                "    identities: [{type: zello, channel: channel, account_id: alice}]\n"
                "  - user_id: bob\n"
                f"    identities: [{{type: zello, channel: channel, account_id: {other_sender}}}]\n",
            )
        )


def test_guest_does_not_authorize_a_zello_channel(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, "{}"))
    assert config.zello_channels() == {}
    with pytest.raises(ValueError, match="No Zello identities"):
        config.zello_access("Cabin Fever x86")


def test_zello_pair_matching_and_whitespace(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            "zello: {credentials_file: keys.yaml}\n"
            "users: [{user_id: guest, identities: "
            "[{type: zello, channel: ' channel ', account_id: ' Alice '}]}]",
        )
    )
    assert config.zello_access("channel") == ("guest", {"alice"})
    assert config.zello_channels() == {"channel": ("guest", {"alice"})}
    for other_channel in ("Channel", "another-channel"):
        with pytest.raises(ValueError, match="No Zello identities"):
            config.zello_access(other_channel)


def test_old_zello_allowlist_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="authorized_users"):
        load_config(
            write(tmp_path, "zello: {credentials_file: keys.yaml, authorized_users: [alice]}\n")
        )


def test_old_zello_channel_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="channel"):
        load_config(write(tmp_path, "zello: {credentials_file: keys.yaml, channel: game}\n"))


def test_example_configs_are_identical_and_valid() -> None:
    root = Path(__file__).resolve().parents[3]
    example = root / "config.example.yaml"
    packaged = root / "packages/cabin-fever-x86/src/cabin_fever_x86/config.example.yaml"
    assert example.read_bytes() == packaged.read_bytes()
    config = load_config(example)
    assert config.zello is not None
    assert config.zello_channels() == {}


def test_an_unset_env_var_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CF86_TEST_MISSING", raising=False)
    config = load_config(write(tmp_path, "client:\n  host: ${CF86_TEST_MISSING}\n"))
    assert config.client.host == "127.0.0.1"


def test_a_typo_is_reported_rather_than_ignored(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="provder"):
        load_config(write(tmp_path, "server:\n  ai_client:\n    provder: openai\n"))


def test_a_missing_file_is_only_an_error_when_asked_for_by_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nowhere.yaml")
