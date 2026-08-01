import pytest

from cabin_fever_x86._home import CONFIG_NAME, DATA_DIR, HOME_ENV_VAR, VM_DIR
from cabin_fever_x86._main import (
    DEFAULT_WEB_PORT,
    IMAGE,
    LauncherConfigError,
    _package_locator,
    _parse_args,
    main,
    run,
)


@pytest.fixture(autouse=True)
def _never_touch_the_real_home(tmp_path, monkeypatch):
    # main() creates its home directory as a side effect, so without this the
    # suite writes into whoever is running its ~/.config.
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _never_boot_a_vm(monkeypatch):
    # A GitHub runner may have no /dev/kvm, and TCG emulation is far too slow
    # to wait on. Anything that needs a real guest belongs in its own workflow.
    def refuse(*args, **kwargs):
        raise AssertionError("the test suite must not boot a VM")

    monkeypatch.setattr("cabin_fever_x86._main.Sandbox", refuse)


@pytest.fixture(autouse=True)
def _never_check_the_real_pypi(monkeypatch):
    monkeypatch.setattr("cabin_fever_x86._main.print_upgrade_notice", lambda *args: None)


def test_defaults_when_nothing_is_passed():
    args = _parse_args([])
    assert args.home is None
    assert args.config is None
    assert args.port == DEFAULT_WEB_PORT
    assert args.rebuild is False


def test_port_is_an_int():
    assert _parse_args(["--port", "9000"]).port == 9000


def test_rebuild_is_opt_in():
    assert _parse_args(["--rebuild"]).rebuild is True


@pytest.mark.asyncio
async def test_rebuild_ignores_an_existing_save(tmp_path, monkeypatch):
    config = tmp_path / CONFIG_NAME
    config.write_text("client: {}\n", encoding="utf-8")
    seen = {}

    class FakeSandbox:
        def __init__(self, **kwargs):
            seen["image"] = kwargs["image"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def initialize(sandbox, package_locator):
        seen["initialized"] = True

    async def save(sandbox, home):
        seen["saved"] = True
        return home / "vm" / "prepared"

    async def attach(sandbox, home, config):
        return "/guest/config.yaml"

    async def start_server(sandbox, guest_config, exports):
        pass

    async def serve(sandbox, guest_config, exports):
        return 0

    monkeypatch.setattr("cabin_fever_x86._main.Sandbox", FakeSandbox)
    monkeypatch.setattr("cabin_fever_x86._main.has_save", lambda home: True)
    monkeypatch.setattr("cabin_fever_x86._main.environment", lambda config, environ: ({}, []))
    monkeypatch.setattr("cabin_fever_x86._main.initialize", initialize)
    monkeypatch.setattr("cabin_fever_x86._main.save", save)
    monkeypatch.setattr("cabin_fever_x86._main.attach", attach)
    monkeypatch.setattr("cabin_fever_x86._main.start_server", start_server)
    monkeypatch.setattr("cabin_fever_x86._main.serve", serve)

    await run(tmp_path, config, DEFAULT_WEB_PORT, rebuild=True)

    assert seen == {"image": IMAGE, "initialized": True, "saved": True}


def test_package_locator_is_optional(tmp_path):
    config = tmp_path / CONFIG_NAME
    config.write_text("client: {}\n", encoding="utf-8")

    assert _package_locator(config, {}) is None


def test_package_locator_is_read_and_environment_is_resolved(tmp_path):
    config = tmp_path / CONFIG_NAME
    config.write_text(
        "launcher:\n  package_locator: ${CORE_ARCHIVE}#subdirectory=core\n", encoding="utf-8"
    )

    assert _package_locator(config, {"CORE_ARCHIVE": "https://example.test/core.tgz"}) == (
        "https://example.test/core.tgz#subdirectory=core"
    )


@pytest.mark.parametrize("body", ["launcher: nope\n", "launcher:\n  package_locator: 42\n"])
def test_invalid_launcher_settings_are_reported(tmp_path, body):
    config = tmp_path / CONFIG_NAME
    config.write_text(body, encoding="utf-8")

    with pytest.raises(LauncherConfigError, match="launcher"):
        _package_locator(config, {})


def test_the_home_is_prepared_and_handed_on(tmp_path, monkeypatch):
    seen = {}

    async def record(home, config, port, rebuild=False):
        seen.update(home=home, config=config, port=port, rebuild=rebuild)

    monkeypatch.setattr("cabin_fever_x86._main.run", record)
    monkeypatch.setattr(
        "sys.argv",
        ["cf86", "--home", str(tmp_path / "elsewhere"), "--port", "9001", "--rebuild"],
    )

    main()

    assert seen["home"] == tmp_path / "elsewhere"
    assert seen["config"] == tmp_path / "elsewhere" / CONFIG_NAME
    assert seen["port"] == 9001
    assert seen["rebuild"] is True
    assert (tmp_path / "elsewhere" / VM_DIR).is_dir()
    assert (tmp_path / "elsewhere" / DATA_DIR).is_dir()


def test_a_config_is_written_so_a_first_run_needs_no_setup(tmp_path, monkeypatch):
    async def nothing(home, config, port, rebuild=False):
        pass

    monkeypatch.setattr("cabin_fever_x86._main.run", nothing)
    monkeypatch.setattr("sys.argv", ["cf86"])

    main()  # no --config, and none exists yet: this must not be an error

    assert (tmp_path / "home" / CONFIG_NAME).is_file()


def test_an_explicit_config_that_is_not_there_is_reported(tmp_path, monkeypatch, capsys):
    # Taken at its word: a typo'd --config should say so, not quietly fall back
    # to the one in the home directory.
    monkeypatch.setattr("sys.argv", ["cf86", "--config", str(tmp_path / "nope.yaml")])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 1
    assert "no config file" in capsys.readouterr().err
