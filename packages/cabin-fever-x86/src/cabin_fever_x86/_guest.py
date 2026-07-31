"""Getting the guest ready, and keeping it ready.

``guest_init.sh`` ships beside this module and is streamed into the guest's
shell. Piping it in rather than copying a file over means there is nothing to
place, nothing to chmod, and nothing left behind — and the script stays
editable in a checkout without rebuilding anything.

Init is slow: it builds a frotz fork from source and installs sixty packages.
So it runs once. When the script reports success the disk is saved under the
home directory's ``vm`` folder, and every later start boots from that save
instead. Delete the save to force a rebuild.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path

import yaml
from quicksand import Sandbox

from cabin_fever_x86._home import DATA_DIR, VM_DIR

#: The script run inside the guest, shipped as package data.
GUEST_INIT = "guest_init.sh"

#: Printed by that script as its last act. The save only happens if this is
#: seen, so a partial init cannot be frozen and reused forever.
SENTINEL = "GUEST INIT COMPLETE"

#: Heredoc delimiters. Quoted where they are used, so the guest's shell expands
#: nothing on the way in and the text arrives exactly as written. The config
#: needs its own: ``${ELEVENLABS_API_KEY}`` in there is for the game to resolve,
#: not for a shell to eat on the way past.
INIT_MARKER = "CABIN_FEVER_X86_GUEST_INIT"
CONFIG_MARKER = "CABIN_FEVER_X86_CONFIG"

#: Long enough to build jericho from source on a slow machine, short enough
#: that a wedged guest does not hang the launcher forever.
INIT_TIMEOUT = 900.0

#: The name of the prepared save, under ``<home>/vm/``.
SAVE_NAME = "cf86"

#: Written by quicksand into every save directory; its presence is what makes
#: a save loadable rather than a half-written directory.
SAVE_MANIFEST = "manifest.json"

#: Where things land inside the guest.
GUEST_ROOT = "/cabin-fever-x86"
GUEST_DATA = f"{GUEST_ROOT}/{DATA_DIR}"
GUEST_CONFIG = f"{GUEST_ROOT}/config.yaml"

#: The web client's port inside the guest. Fixed, because the only way to it is
#: a forward the launcher sets up; the host side of that forward is what the
#: --port flag moves. The game server's own port is never forwarded, so it is
#: reachable only from inside the guest.
GUEST_WEB_PORT = 8000

#: The background server's output. Under the data mount, so it can be read on
#: the host while the game is running and after the guest is gone.
SERVER_LOG = f"{GUEST_DATA}/server.log"

#: How long the server is given to fall over before the web client is started.
SERVER_SETTLE_SECONDS = 3

#: A game night, with room to spare. execute() insists on a number, and the
#: web client is meant to run until someone closes it.
SERVE_TIMEOUT = 24 * 60 * 60.0

#: ``${ENV_VAR_NAME}`` references in the config. Deliberately the same pattern
#: as ``cabin_fever_x86_core.config``, and deliberately a copy of it: the
#: launcher cannot import core, because core is not installed on this side.
#: If that pattern ever changes, this one has to change with it.
ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class GuestInitError(RuntimeError):
    """The init script did not finish cleanly."""


def guest_init_script() -> str:
    """Return the init script, as shipped in this package."""
    return files(__package__).joinpath(GUEST_INIT).read_text(encoding="utf-8")


def init_command(script: str) -> str:
    """Wrap *script* so a shell in the guest reads it from its own stdin."""
    if INIT_MARKER in script:
        # The heredoc would end early and the rest of the script would be run
        # as commands by the outer shell. Cheap to check, miserable to debug.
        raise ValueError(f"{GUEST_INIT} must not contain {INIT_MARKER!r}")
    return f"cat <<'{INIT_MARKER}' | bash\n{script}\n{INIT_MARKER}\n"


def write_command(text: str, destination: str, marker: str) -> str:
    """Build a command that writes *text* to *destination* inside the guest."""
    if marker in text:
        raise ValueError(f"content bound for {destination} must not contain {marker!r}")
    return f"cat <<'{marker}' > {destination}\n{text}\n{marker}\n"


def save_path(home: Path) -> Path:
    """Return where the prepared guest is kept for this home directory."""
    return home / VM_DIR / SAVE_NAME


def has_save(home: Path) -> bool:
    """Whether a prepared guest is already sitting in *home*."""
    return (save_path(home) / SAVE_MANIFEST).is_file()


def image_for(home: Path, base_image: str) -> str:
    """Return what to boot: the prepared save if there is one, else *base_image*.

    The save is passed as a path rather than a name — quicksand looks names up
    under ``.quicksand/sandboxes`` in the working directory and the user's home,
    and this one lives in neither.
    """
    return str(save_path(home)) if has_save(home) else base_image


async def initialize(sandbox: Sandbox) -> None:
    """Run the init script in *sandbox*, echoing its output as it arrives.

    Raises if the script fails, or if it exits cleanly without printing the
    sentinel — a silent early return would otherwise be saved and reused.
    """
    seen: list[str] = []

    def note(chunk: str) -> None:
        seen.append(chunk)
        # Flushed, or a piped stdout holds the whole init in a buffer and
        # "streaming" arrives all at once when the guest is already done.
        print(chunk, end="", flush=True)

    result = await sandbox.execute(
        init_command(guest_init_script()),
        timeout=INIT_TIMEOUT,
        on_stdout=note,
        on_stderr=lambda chunk: print(chunk, end="", flush=True, file=sys.stderr),
    )
    if result.exit_code != 0:
        raise GuestInitError(f"{GUEST_INIT} exited {result.exit_code}")

    # Joined rather than checked per chunk: the sentinel can arrive split
    # across two reads of the stream.
    if SENTINEL not in "".join(seen):
        raise GuestInitError(f"{GUEST_INIT} never printed {SENTINEL!r}")


async def save(sandbox: Sandbox, home: Path) -> Path:
    """Freeze the prepared guest under *home* so the next start can skip init."""
    destination = home / VM_DIR
    await sandbox.save(SAVE_NAME, workspace=destination)
    return save_path(home)


async def attach(sandbox: Sandbox, home: Path, config: Path) -> str:
    """Mount the host's data into a running guest and give it the config.

    Returns the config's path as the guest sees it.

    Data is a mount because it is written from inside and has to outlive the
    guest. The config goes in as a copy instead: mounts are whole directories,
    and mounting the config's folder would hand the guest a view of everything
    beside it — the prepared save included. It is rewritten on every start, so
    editing it on the host and starting again is all it takes.
    """
    await sandbox.mount(str((home / DATA_DIR).resolve()), GUEST_DATA)

    text = config.read_text(encoding="utf-8")
    result = await sandbox.execute(write_command(text, GUEST_CONFIG, CONFIG_MARKER))
    if result.exit_code != 0:
        raise GuestInitError(f"could not write {GUEST_CONFIG} in the guest")
    return GUEST_CONFIG


def referenced_variables(config_text: str) -> list[str]:
    """Return the environment variables the config refers to, in order, once each.

    Only string *values* count, which is what core expands. The file's own
    comments explain the ``${ENV_VAR_NAME}`` syntax using that syntax, and
    scanning the raw text would dutifully go looking for a variable called
    ENV_VAR_NAME and report it missing.
    """
    try:
        parsed = yaml.safe_load(config_text)
    except yaml.YAMLError:
        # Malformed: let core be the one to complain about it, and fall back to
        # the blunt scan meanwhile. Over-matching here costs a warning, nothing
        # more.
        parsed = None
        found = ENV_REFERENCE.findall(config_text)
    else:
        found = []

    def walk(node: object) -> None:
        if isinstance(node, str):
            found.extend(ENV_REFERENCE.findall(node))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(key)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(parsed)
    return list(dict.fromkeys(found))


def environment(config: Path, environ: Mapping[str, str] | None = None) -> tuple[str, list[str]]:
    """Return shell exports for what the config needs, and what was not there.

    The config's ``${OPENAI_API_KEY}`` references are resolved by whichever
    process loads the file — and that process is now inside the guest, where
    none of the host's environment reaches. So the variables the config asks
    for are carried across explicitly, and nothing else is: the guest has no
    business seeing the rest of the host's environment.
    """
    environ = os.environ if environ is None else environ

    exports: list[str] = []
    missing: list[str] = []
    for name in referenced_variables(config.read_text(encoding="utf-8")):
        if name in environ:
            # Quoted, because these are secrets of arbitrary shape and one
            # stray character would otherwise be a shell injection.
            exports.append(f"export {name}={shlex.quote(environ[name])}")
        else:
            missing.append(name)
    return "\n".join(exports), missing


def server_command(guest_config: str, exports: str = "") -> str:
    """Build the command that puts the game server into the background.

    ``setsid`` and ``nohup`` because the shell that starts it goes away as soon
    as this call returns, and the server has to outlive it.
    """
    return f"""
cd {GUEST_ROOT}
. .venv/bin/activate
{exports}
setsid nohup cf86-server --config {guest_config} > {SERVER_LOG} 2>&1 &
echo $! > {GUEST_ROOT}/server.pid
# A bad key or an unreadable config kills the server in the first second. Far
# better to say so here than to hand someone a browser tab that never answers.
sleep {SERVER_SETTLE_SECONDS}
if ! kill -0 "$(cat {GUEST_ROOT}/server.pid)" 2>/dev/null; then
    echo "the game server exited on startup:" >&2
    tail -20 {SERVER_LOG} >&2
    exit 1
fi
"""


def web_command(guest_config: str, exports: str = "") -> str:
    """Build the command that runs the web client in the foreground.

    Bound to 0.0.0.0 *inside the guest* — the forward arrives on the guest's
    NIC rather than its loopback, so binding 127.0.0.1 there would refuse it.
    The only route in is still the forward, which quicksand pins to the host's
    loopback and nothing else.
    """
    return f"""
cd {GUEST_ROOT}
. .venv/bin/activate
{exports}
exec cf86-web --config {guest_config} --web-host 0.0.0.0 --web-port {GUEST_WEB_PORT}
"""


async def start_server(sandbox: Sandbox, guest_config: str, exports: str = "") -> None:
    """Start the game server in the background, and check it stayed up."""
    result = await sandbox.execute(
        server_command(guest_config, exports),
        shell="/bin/bash",
        timeout=SERVER_SETTLE_SECONDS + 30,
        on_stderr=lambda chunk: print(chunk, end="", flush=True, file=sys.stderr),
    )
    if result.exit_code != 0:
        raise GuestInitError("the game server did not start")


async def serve(sandbox: Sandbox, guest_config: str, exports: str = "") -> int:
    """Run the web client in the foreground until it stops. Returns its code."""
    result = await sandbox.execute(
        web_command(guest_config, exports),
        shell="/bin/bash",
        timeout=SERVE_TIMEOUT,
        on_stdout=lambda chunk: print(chunk, end="", flush=True),
        on_stderr=lambda chunk: print(chunk, end="", flush=True, file=sys.stderr),
    )
    return result.exit_code
