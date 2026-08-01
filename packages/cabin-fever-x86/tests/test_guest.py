import re
import shlex

import pytest

from cabin_fever_x86._guest import (
    CONFIG_MARKER,
    GUEST_CONFIG,
    GUEST_DATA,
    GUEST_INIT,
    GUEST_WEB_PORT,
    INIT_MARKER,
    SAVE_MANIFEST,
    SAVE_NAME,
    SENTINEL,
    SERVER_LOG,
    GuestInitError,
    attach,
    environment,
    guest_init_script,
    has_save,
    image_for,
    init_command,
    initialize,
    referenced_variables,
    save,
    save_path,
    serve,
    server_command,
    start_server,
    web_command,
    write_command,
)
from cabin_fever_x86._home import VM_DIR, default_config_template


class FakeSandbox:
    """Records what would have been run, without a guest to run it in."""

    def __init__(self, exit_code: int = 0, output: str | None = None):
        self.exit_code = exit_code
        self.output = f"...\n{SENTINEL}\n" if output is None else output
        self.commands: list[str] = []
        self.saves: list[tuple[str, object]] = []
        self.mounts: list[tuple[str, str, bool]] = []

    async def execute(self, command, timeout=None, on_stdout=None, on_stderr=None, **kwargs):
        self.commands.append(command)
        if on_stdout:
            for chunk in self.output:  # one character at a time: the worst case
                on_stdout(chunk)
        return type("Result", (), {"exit_code": self.exit_code, "stdout": "", "stderr": ""})()

    async def save(self, name, workspace=None, **kwargs):
        self.saves.append((name, workspace))
        (workspace / name).mkdir(parents=True, exist_ok=True)
        (workspace / name / SAVE_MANIFEST).write_text("{}")

    async def mount(self, host, guest, readonly=False):
        self.mounts.append((host, guest, readonly))
        return object()


def test_the_script_ships_with_the_package():
    assert SENTINEL in guest_init_script()


def test_the_script_is_delivered_over_stdin():
    command = init_command("echo hi\n")

    assert command.splitlines()[0] == f"cat <<'{INIT_MARKER}' | bash"
    assert "echo hi" in command
    assert command.splitlines()[-1] == INIT_MARKER


def test_the_delimiter_is_quoted_so_the_guest_expands_nothing():
    # Unquoted, $HOME and `date` would be substituted by the guest's shell
    # before bash ever saw the script.
    assert f"<<'{INIT_MARKER}'" in init_command("echo $HOME\n")


def test_a_script_containing_the_delimiter_is_refused():
    with pytest.raises(ValueError, match=INIT_MARKER):
        init_command(f"echo one\n{INIT_MARKER}\nrm -rf /\n")


async def test_initialize_runs_the_shipped_script():
    sandbox = FakeSandbox()

    await initialize(sandbox)

    assert len(sandbox.commands) == 1
    assert SENTINEL in sandbox.commands[0]


async def test_a_failing_script_is_not_swallowed():
    with pytest.raises(GuestInitError, match=GUEST_INIT):
        await initialize(FakeSandbox(exit_code=1))


async def test_a_clean_exit_without_the_sentinel_is_still_a_failure():
    # Otherwise a script that returned early would be frozen into the save and
    # reused forever.
    with pytest.raises(GuestInitError, match=SENTINEL):
        await initialize(FakeSandbox(output="did some things\n"))


async def test_the_sentinel_is_found_even_when_split_across_reads():
    # FakeSandbox streams a character at a time, so this only passes if the
    # chunks are joined before looking.
    await initialize(FakeSandbox(output=f"noise\n{SENTINEL}\n"))


def test_no_save_in_a_fresh_home(tmp_path):
    assert has_save(tmp_path) is False
    assert save_path(tmp_path) == tmp_path / VM_DIR / SAVE_NAME


def test_a_half_written_save_does_not_count(tmp_path):
    # The directory exists but quicksand never finished writing the manifest.
    (tmp_path / VM_DIR / SAVE_NAME).mkdir(parents=True)

    assert has_save(tmp_path) is False


async def test_saving_makes_the_next_start_use_it(tmp_path):
    sandbox = FakeSandbox()
    assert image_for(tmp_path, "quicksand-agent") == "quicksand-agent"

    written = await save(sandbox, tmp_path)

    assert sandbox.saves == [(SAVE_NAME, tmp_path / VM_DIR)]
    assert written == tmp_path / VM_DIR / SAVE_NAME
    assert has_save(tmp_path)
    # A path, not a name: quicksand only looks names up in two fixed places,
    # and the vm folder is neither.
    assert image_for(tmp_path, "quicksand-agent") == str(tmp_path / VM_DIR / SAVE_NAME)


def test_a_file_is_written_over_stdin_too():
    command = write_command("client: {}\n", "/somewhere/config.yaml", CONFIG_MARKER)

    assert command.splitlines()[0] == f"cat <<'{CONFIG_MARKER}' > /somewhere/config.yaml"
    assert "client: {}" in command
    assert command.splitlines()[-1] == CONFIG_MARKER


def test_the_config_is_not_expanded_on_the_way_in():
    # config.example.yaml is full of ${OPENAI_API_KEY}. Those are for the game
    # to resolve inside the guest, not for a shell to swallow in transit.
    command = write_command("api_key: ${OPENAI_API_KEY}\n", GUEST_CONFIG, CONFIG_MARKER)

    assert f"<<'{CONFIG_MARKER}'" in command
    assert "${OPENAI_API_KEY}" in command


def test_content_containing_the_delimiter_is_refused():
    with pytest.raises(ValueError, match=CONFIG_MARKER):
        write_command(f"a: 1\n{CONFIG_MARKER}\nrm -rf /\n", GUEST_CONFIG, CONFIG_MARKER)


async def test_attach_mounts_data_and_copies_the_config_in(tmp_path):
    sandbox = FakeSandbox()
    config = tmp_path / "config.yaml"
    config.write_text("client: {port: 5000}\n")
    (tmp_path / "data").mkdir()

    guest_config = await attach(sandbox, tmp_path, config)

    # Data is mounted; the config is not — nothing beside it is exposed.
    assert sandbox.mounts == [(str(tmp_path / "data"), GUEST_DATA, False)]
    assert guest_config == GUEST_CONFIG
    assert f"> {GUEST_CONFIG}" in sandbox.commands[-1]
    assert "client: {port: 5000}" in sandbox.commands[-1]


def test_only_what_the_config_asks_for_is_carried_across(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("a: ${OPENAI_API_KEY}\nb: ${ELEVENLABS_API_KEY}\nc: literal\n")

    exports, missing = environment(
        config, {"OPENAI_API_KEY": "sk-abc", "ELEVENLABS_API_KEY": "el-xyz", "SECRET": "no"}
    )

    # Quoting is shlex's business — assert the assignments, not their spelling.
    assert shlex.split(exports) == [
        "export",
        "OPENAI_API_KEY=sk-abc",
        "export",
        "ELEVENLABS_API_KEY=el-xyz",
    ]
    assert "SECRET" not in exports  # the guest gets what it needs, nothing more
    assert missing == []


def test_references_in_comments_are_not_mistaken_for_settings():
    # config.example.yaml documents the ${ENV_VAR_NAME} syntax in a comment,
    # using that syntax. Scanning raw text goes looking for ENV_VAR_NAME.
    text = (
        "# ${ENV_VAR_NAME} references are resolved against the environment.\n"
        "server:\n  ai_client:\n    api_key: ${OPENAI_API_KEY}\n"
    )

    assert referenced_variables(text) == ["OPENAI_API_KEY"]


def test_the_shipped_template_refers_only_to_real_keys():
    assert referenced_variables(default_config_template()) == [
        "ELEVENLABS_API_KEY",
        "OPENAI_API_KEY",
    ]


def test_a_variable_is_carried_once_even_if_referenced_twice(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("a: ${TOKEN}\nb: ${TOKEN}\n")

    assert referenced_variables(config.read_text()) == ["TOKEN"]
    exports, _ = environment(config, {"TOKEN": "t"})
    assert exports.count("export TOKEN=") == 1


def test_unset_variables_are_reported_rather_than_exported_empty(tmp_path):
    # Core treats an unset reference as "key absent, use the default". Exporting
    # an empty string instead would override that default with nothing.
    config = tmp_path / "config.yaml"
    config.write_text("a: ${NOT_SET_ANYWHERE}\n")

    exports, missing = environment(config, {})

    assert exports == ""
    assert missing == ["NOT_SET_ANYWHERE"]


def test_awkward_values_cannot_break_out_of_the_command(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("a: ${NASTY}\n")

    exports, _ = environment(config, {"NASTY": "it's; rm -rf / #"})

    # Quoted whole; the semicolon and quote are data, not syntax.
    assert exports == """export NASTY='it'"'"'s; rm -rf / #'"""
    assert shlex.split(exports)[1] == "NASTY=it's; rm -rf / #"


def test_both_processes_get_the_environment():
    exports = "export OPENAI_API_KEY='sk-abc'"

    assert exports in server_command(GUEST_CONFIG, exports)
    assert exports in web_command(GUEST_CONFIG, exports)


def test_the_server_is_put_into_the_background_and_survives_its_shell():
    command = server_command(GUEST_CONFIG)

    assert "cf86-server" in command
    assert command.count("&\n") == 1  # backgrounded
    assert "setsid" in command and "nohup" in command
    assert SERVER_LOG in command
    # Checked, not assumed: a server that died on startup must fail here.
    assert "kill -0" in command
    assert "exit 1" in command


def test_the_server_is_never_forwarded():
    # Only the web client's port is given a route in from the host.
    assert f"--web-port {GUEST_WEB_PORT}" in web_command(GUEST_CONFIG)
    assert "--port" not in web_command(GUEST_CONFIG)


def test_the_web_client_binds_where_the_forward_arrives():
    # The forward lands on the guest's NIC, not its loopback, so 127.0.0.1
    # inside the guest would refuse it.
    assert "--web-host 0.0.0.0" in web_command(GUEST_CONFIG)


def test_the_web_client_replaces_its_shell():
    # exec, so signals and exit codes are the web client's own rather than a
    # wrapper shell's.
    assert "exec cf86-web" in web_command(GUEST_CONFIG)


async def test_serve_reports_the_web_clients_exit_code():
    assert await serve(FakeSandbox(exit_code=3), GUEST_CONFIG) == 3


async def test_a_server_that_dies_on_startup_is_reported():
    with pytest.raises(GuestInitError, match="game server"):
        await start_server(FakeSandbox(exit_code=1), GUEST_CONFIG)


async def test_a_config_that_cannot_be_written_is_reported(tmp_path):
    sandbox = FakeSandbox(exit_code=1)
    config = tmp_path / "config.yaml"
    config.write_text("client: {}\n")
    (tmp_path / "data").mkdir()

    with pytest.raises(GuestInitError, match=re.escape(GUEST_CONFIG)):
        await attach(sandbox, tmp_path, config)
