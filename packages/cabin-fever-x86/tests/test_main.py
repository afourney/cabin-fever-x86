import pytest

from cabin_fever_x86._home import CONFIG_NAME, DATA_DIR, HOME_ENV_VAR, VM_DIR
from cabin_fever_x86._main import DEFAULT_WEB_PORT, _parse_args, main


@pytest.fixture(autouse=True)
def _never_touch_the_real_home(tmp_path, monkeypatch):
    # main() creates its home directory as a side effect, so without this the
    # suite writes into whoever is running it's ~/.config.
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _never_boot_a_vm(monkeypatch):
    # A GitHub runner may have no /dev/kvm, and TCG emulation is far too slow
    # to wait on. Anything that needs a real guest belongs in its own workflow.
    def refuse(*args, **kwargs):
        raise AssertionError("the test suite must not boot a VM")

    monkeypatch.setattr("cabin_fever_x86._main.Sandbox", refuse)


def test_defaults_when_nothing_is_passed():
    args = _parse_args([])
    assert args.home is None
    assert args.config is None
    assert args.port == DEFAULT_WEB_PORT


def test_port_is_an_int():
    assert _parse_args(["--port", "9000"]).port == 9000


def test_the_home_is_prepared_and_handed_on(tmp_path, monkeypatch):
    seen = {}

    async def record(home, config, port):
        seen.update(home=home, config=config, port=port)

    monkeypatch.setattr("cabin_fever_x86._main.run", record)
    monkeypatch.setattr(
        "sys.argv", ["cf86", "--home", str(tmp_path / "elsewhere"), "--port", "9001"]
    )

    main()

    assert seen["home"] == tmp_path / "elsewhere"
    assert seen["config"] == tmp_path / "elsewhere" / CONFIG_NAME
    assert seen["port"] == 9001
    assert (tmp_path / "elsewhere" / VM_DIR).is_dir()
    assert (tmp_path / "elsewhere" / DATA_DIR).is_dir()


def test_a_config_is_written_so_a_first_run_needs_no_setup(tmp_path, monkeypatch):
    async def nothing(home, config, port):
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
