"""Where the launcher keeps its things, on whatever platform it woke up on.

One directory holds all of it — config, the VM image and its saves, and the
game data that has to outlive any particular guest:

    <home>/config.yaml   settings, mounted into the guest read-only
    <home>/launcher-pypi-version-cache.json  daily cache of the latest PyPI version
    <home>/vm/           the sandbox image and its saves
    <home>/data/         games, sessions, transcripts, saved games

The split matters because the guest is disposable and ``data`` is not. A VM
that is torn down at the end of a session takes everything written inside it
along, so anything worth keeping lives out here on the host and is mounted in.
"""

from __future__ import annotations

import os
import sys
from importlib.resources import files
from pathlib import Path

#: Overrides the platform default. Handy for keeping a game night's worth of
#: state somewhere other than the usual place, and for tests.
HOME_ENV_VAR = "CABIN_FEVER_X86_HOME"

#: The directory's name under whichever per-user config root the platform uses.
APP_DIR_NAME = "cabin-fever-x86"

#: Subdirectories, created alongside the config.
VM_DIR = "vm"
DATA_DIR = "data"

#: The config written on first run, shipped with this package.
CONFIG_NAME = "config.yaml"
CONFIG_TEMPLATE = "config.example.yaml"


def default_home() -> Path:
    """Return the per-user directory for this platform, existing or not."""
    if override := os.environ.get(HOME_ENV_VAR):
        return Path(override).expanduser()

    if sys.platform == "win32":
        # APPDATA is the roaming profile, which is where per-user application
        # settings belong. It is set on any normal login; the fallback is for
        # service accounts and stripped-down containers.
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        # XDG, which specifies ~/.config as the default when unset.
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"

    return root / APP_DIR_NAME


def default_config_template() -> str:
    """Return the sample config, as shipped in this package."""
    return files(__package__).joinpath(CONFIG_TEMPLATE).read_text(encoding="utf-8")


def prepare_home(home: Path | None = None) -> Path:
    """Create the home directory and its contents if they are not there yet.

    Returns the directory. Safe to call on every run: existing directories are
    left alone, and an existing config is never written over — someone's API
    keys are in there.
    """
    home = (home or default_home()).expanduser()

    for path in (home, home / VM_DIR, home / DATA_DIR):
        path.mkdir(parents=True, exist_ok=True)

    config = home / CONFIG_NAME
    if not config.exists():
        config.write_text(default_config_template(), encoding="utf-8")

    return home
