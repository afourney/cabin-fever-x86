"""Entry point for the Cabin Fever x86 launcher."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

import yaml
from quicksand import NetworkMode, PortForward, Sandbox

from cabin_fever_x86 import __version__
from cabin_fever_x86._guest import (
    ENV_REFERENCE,
    GUEST_WEB_PORT,
    attach,
    environment,
    has_save,
    image_for,
    initialize,
    save,
    serve,
    start_server,
)
from cabin_fever_x86._home import (
    CONFIG_NAME,
    HOME_ENV_VAR,
    default_home,
    prepare_home,
)
from cabin_fever_x86._updates import print_upgrade_notice

#: What the guest boots: quicksand-ubuntu plus Python 3.12, uv, and a C
#: toolchain. The toolchain is not optional — installing the game builds a
#: frotz fork from source, since jericho ships no wheels.
IMAGE = "quicksand-agent"

DEFAULT_WEB_PORT = 8000


class LauncherConfigError(ValueError):
    """Raised when the host-side launcher settings are malformed."""


def _package_locator(config: Path, environ: Mapping[str, str]) -> str | None:
    """Read and resolve the one config value interpreted by the launcher."""
    try:
        raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LauncherConfigError(f"could not parse YAML: {exc}") from exc
    except OSError as exc:
        raise LauncherConfigError(f"could not read file: {exc}") from exc

    if not isinstance(raw, dict):
        raise LauncherConfigError("the top level must be a mapping")
    launcher = raw.get("launcher", {})
    if launcher is None:
        launcher = {}
    if not isinstance(launcher, dict):
        raise LauncherConfigError("launcher must be a mapping")
    locator = launcher.get("package_locator")
    if locator is None:
        return None
    if not isinstance(locator, str):
        raise LauncherConfigError("launcher.package_locator must be a string")

    resolved = ENV_REFERENCE.sub(lambda match: environ.get(match.group(1), ""), locator)
    return resolved or None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cabin Fever x86 Launcher",
    )
    parser.add_argument(
        "--home",
        default=None,
        help=(
            f"Where config, the VM, and game data live. Created if missing. "
            f"Overrides ${HOME_ENV_VAR} (default: {default_home()})."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file, mounted into the guest (default: <home>/{CONFIG_NAME}).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"Host port to serve the web application on (default: {DEFAULT_WEB_PORT}).",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the prepared guest before launching.",
    )
    return parser.parse_args(argv)


async def _until_eof() -> None:
    """Return when stdin reaches EOF — someone pressing Ctrl-D at the prompt.

    Waits forever when stdin is not a terminal, so a run with its input closed
    or redirected does not mistake that for the user hanging up.
    """
    if sys.stdin is None or not sys.stdin.isatty():
        await asyncio.Event().wait()
        return

    loop = asyncio.get_running_loop()
    hung_up = asyncio.Event()

    def watch() -> None:
        with contextlib.suppress(Exception):  # stdin closed under us; same thing
            for _ in sys.stdin:
                pass
        loop.call_soon_threadsafe(hung_up.set)

    # A daemon thread rather than a reader on the event loop: asyncio cannot
    # watch a console handle on Windows, and this has to work there too.
    threading.Thread(target=watch, name="stdin-watch", daemon=True).start()
    await hung_up.wait()


async def _until_interrupt() -> None:
    """Return on Ctrl-C, where the platform lets us hear about it that way.

    Handling SIGINT as an event rather than an exception means the sandbox's
    context manager unwinds normally and the guest is shut down properly. Where
    that is not available, the KeyboardInterrupt in main() is the fallback.
    """
    interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, interrupted.set)
    except (NotImplementedError, AttributeError, RuntimeError):
        await asyncio.Event().wait()  # Windows: let KeyboardInterrupt do it
        return

    try:
        await interrupted.wait()
    finally:
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGINT)


async def run(home: Path, config: Path, port: int, rebuild: bool = False) -> None:
    """Boot the guest, start the game inside it, and serve until it stops."""
    # The config's ${...} references are resolved wherever it is loaded,
    # which is now inside the guest. Carry across exactly what it asks for.
    environ = os.environ.copy()
    exports, missing = environment(config, environ)
    if missing:
        if not sys.stdin.isatty():
            print(
                "error: required environment variables are missing and stdin is not interactive; "
                f"please set: {', '.join(missing)}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        print(
            "Cabin Fever x86 needs some environment variables to run, but they are not set. "
            "Please provide them now (or set them in your shell and restart).\n"
        )
        import getpass

        for name in missing:
            value = ""
            while not value:
                value = getpass.getpass(f"Please enter a value for {name}: ").strip()
            environ[name] = value

        exports, missing = environment(config, environ)

        if missing:
            print(f"error: still missing {', '.join(missing)} after prompting.", file=sys.stderr)
            raise SystemExit(1)

    try:
        package_locator = _package_locator(config, environ)
    except LauncherConfigError as exc:
        print(f"error: {config}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    prepared = has_save(home) and not rebuild
    if rebuild:
        print("Rebuilding the prepared guest. This takes a few minutes.")
    elif prepared:
        print("Booting the prepared guest.")
    else:
        print("No prepared guest yet; building one. This takes a few minutes, once.")

    # The guest reaches the internet: it installs the game from a source
    # archive, and the companion talks to the model provider itself.
    # Quicksand's default is MOUNTS_ONLY, so this is a deliberate widening —
    # and it means anything that escapes the interpreter has a way out.
    #
    # One forward, and only one: the web client. The game server listens inside
    # the guest and stays there. Quicksand pins the host end to 127.0.0.1 and
    # offers no way to widen it, so this is a loopback port by construction.
    async with Sandbox(
        image=IMAGE if rebuild else image_for(home, IMAGE),
        network_mode=NetworkMode.FULL,
        port_forwards=[PortForward(host=port, guest=GUEST_WEB_PORT)],
    ) as sandbox:
        if not prepared:
            await initialize(sandbox, package_locator)
            # Saved before anything is mounted or written, so the frozen disk
            # holds the install and none of this particular night — nor the
            # config, which by now has API keys in it.
            print(f"Saved the prepared guest to {await save(sandbox, home)}")

        guest_config = await attach(sandbox, home, config)

        await start_server(sandbox, guest_config, exports)

        print(f"\nCabin Fever x86 on http://127.0.0.1:{port}")
        print("Ctrl-C or Ctrl-D to hang up.\n")

        web = asyncio.create_task(serve(sandbox, guest_config, exports))
        waiting = [web, asyncio.create_task(_until_eof()), asyncio.create_task(_until_interrupt())]
        done, pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*pending, return_exceptions=True)

        if web in done:
            code = web.result()
            print(f"\nThe radio went quiet (web client exited {code}).")
        else:
            print("\nHanging up.")


def main() -> None:
    args = _parse_args()

    # Header
    print("-" * 60)
    print(" C A B I N   F E V E R   x 8 6 ")
    print("-" * 60)
    print(f"Launcher version {__version__}")
    home = prepare_home(Path(args.home) if args.home else None)
    print(f"Home directory: {home}")
    print("")
    print_upgrade_notice(home, __version__)

    # Ensure the config file exists (whether specified or default).
    config = Path(args.config).expanduser() if args.config else home / CONFIG_NAME
    if not config.is_file():
        print(f"error: no config file at {config}", file=sys.stderr)
        raise SystemExit(1)

    # The fallback for platforms where SIGINT cannot be taken as an event.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(home, config, args.port, rebuild=args.rebuild))


if __name__ == "__main__":
    main()
