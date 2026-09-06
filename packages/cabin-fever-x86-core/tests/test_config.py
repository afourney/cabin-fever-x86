"""A first test, mostly to prove the harness runs.

Config loading is a reasonable thing to pin down first: every entry point goes
through it, and it is pure enough to test without a server, a model, or a
sound card.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cabin_fever_x86_core.config import ConfigError, load_config


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
    assert config.telegram_client.bot_token is None
    assert config.telegram_client.allowed_accounts == []


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
            """telegram_client:
  bot_token: ${CF86_TELEGRAM_TOKEN}
  api_id: 12345
  api_hash: hash
  allowed_accounts: [111, 222]
""",
        )
    )

    assert config.telegram_client.bot_token == "123:secret"
    assert config.telegram_client.api_id == 12345
    assert config.telegram_client.api_hash == "hash"
    assert config.telegram_client.allowed_accounts == [111, 222]


def test_zello_config_is_loaded(tmp_path: Path) -> None:
    config = load_config(
        write(
            tmp_path,
            """zello:
  credentials_file: ~/.apikeys/zello.yaml
  authorized_users: [alice]
""",
        )
    )

    assert config.zello is not None
    assert config.zello.credentials_file == "~/.apikeys/zello.yaml"
    assert config.zello.channel == "Cabin Fever x86"
    assert config.zello.authorized_users == ["alice"]


@pytest.mark.parametrize(
    "body", ["zello: {authorized_users: []}\n", "zello: {credentials_file: keys.yaml}\n"]
)
def test_zello_section_requires_credentials_and_authorized_users(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, body))


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
