import sys
from pathlib import Path

import pytest

from cabin_fever_x86._home import (
    APP_DIR_NAME,
    CONFIG_NAME,
    DATA_DIR,
    HOME_ENV_VAR,
    VM_DIR,
    default_config_template,
    default_home,
    prepare_home,
)


@pytest.fixture(autouse=True)
def _no_inherited_home(monkeypatch):
    # Otherwise a developer with the variable set gets different results
    # than CI does.
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)


def test_linux_follows_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/someone")))

    assert default_home() == Path("/home/someone/.config") / APP_DIR_NAME


def test_linux_honours_an_explicit_xdg_config_home(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/elsewhere/config")

    assert default_home() == Path("/elsewhere/config") / APP_DIR_NAME


def test_macos_uses_application_support(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/someone")))

    assert default_home() == Path("/Users/someone/Library/Application Support") / APP_DIR_NAME


def test_windows_uses_appdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")

    assert default_home() == Path(r"C:\Users\someone\AppData\Roaming") / APP_DIR_NAME


def test_windows_falls_back_when_appdata_is_unset(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/users/someone")))

    assert default_home() == Path("/users/someone/AppData/Roaming") / APP_DIR_NAME


def test_the_environment_variable_wins_on_every_platform(monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "somewhere"))

    for platform in ("linux", "darwin", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        assert default_home() == tmp_path / "somewhere"


def test_prepare_home_creates_everything(tmp_path):
    home = prepare_home(tmp_path / "cf86")

    assert home.is_dir()
    assert (home / VM_DIR).is_dir()
    assert (home / DATA_DIR).is_dir()
    assert (home / CONFIG_NAME).read_text(encoding="utf-8") == default_config_template()


def test_prepare_home_is_safe_to_run_again(tmp_path):
    home = prepare_home(tmp_path / "cf86")
    (home / DATA_DIR / "sessions").mkdir()

    assert prepare_home(home) == home
    assert (home / DATA_DIR / "sessions").is_dir()


def test_an_existing_config_is_never_written_over(tmp_path):
    # It holds API keys by the second run. Clobbering it would be unforgivable.
    home = prepare_home(tmp_path / "cf86")
    (home / CONFIG_NAME).write_text("client:\n  port: 9999\n", encoding="utf-8")

    prepare_home(home)

    assert (home / CONFIG_NAME).read_text(encoding="utf-8") == "client:\n  port: 9999\n"


def test_prepare_home_defaults_to_the_platform_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "default"))

    assert prepare_home() == tmp_path / "default"
    assert (tmp_path / "default" / CONFIG_NAME).is_file()


def test_the_shipped_template_is_the_one_in_the_repository():
    # Two copies exist: config.example.yaml at the repo root, which the docs
    # point at, and the one packaged here, which is what a fresh install gets.
    # They drift silently otherwise. Skipped when running from a wheel.
    repo_copy = Path(__file__).resolve().parents[3] / "config.example.yaml"
    if not repo_copy.is_file():
        pytest.skip("not running from a checkout")

    assert default_config_template() == repo_copy.read_text(encoding="utf-8")
