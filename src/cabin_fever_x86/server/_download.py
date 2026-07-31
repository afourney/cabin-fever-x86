"""Fetching the games the companion plays.

The z-machine suite is a 100MB archive of which only one folder matters, so
the download is kept out of the repository and pulled on first run instead.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

logger = logging.getLogger(__name__)

GAMES_URL = "https://github.com/BYU-PCCL/z-machine-games/archive/master.zip"

# Any of these, set to anything but "0"/"false"/"", means: do not go and get
# 100MB of games. ``CI`` is set by GitHub Actions and by every other runner
# worth the name; ``CF86_NO_DOWNLOAD`` is for saying so deliberately.
NO_DOWNLOAD_VARS = ("CF86_NO_DOWNLOAD", "CI")

# The one folder in the archive worth keeping.
SUITE_DIR = "jericho-game-suite"

# What a game file looks like: .z3 through .z8.
GAME_SUFFIXES = tuple(f".z{version}" for version in range(1, 9))


class DownloadError(Exception):
    """The games could not be fetched or unpacked."""


def has_games(games_dir: Path) -> bool:
    """Whether there is anything playable on the disk already."""
    return games_dir.is_dir() and any(
        path.suffix.lower() in GAME_SUFFIXES for path in games_dir.iterdir()
    )


def downloads_blocked() -> str | None:
    """Name the variable forbidding a download, if one is set."""
    for name in NO_DOWNLOAD_VARS:
        value = os.environ.get(name, "").strip().lower()
        if value and value not in {"0", "false", "no"}:
            return name
    return None


def ensure_games(games_dir: Path, url: str = GAMES_URL) -> int:
    """Make sure the games are on disk, fetching them if they are not.

    Returns how many were installed, so nothing is downloaded twice. Nothing
    is fetched when a no-download variable is set: a test run has no business
    pulling 100MB, and would rather have no games than wait for them.
    """
    if has_games(games_dir):
        return 0

    blocked = downloads_blocked()
    if blocked is not None:
        logger.warning("No games in %s, and %s is set; not downloading", games_dir, blocked)
        return 0

    logger.info("No games in %s; fetching the suite", games_dir)
    return download_games(games_dir, url)


def download_games(games_dir: Path, url: str = GAMES_URL) -> int:
    """Download the archive and unpack the game suite into *games_dir*."""
    games_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "z-machine-games.zip"
        try:
            with urlopen(url) as response, archive.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (URLError, OSError) as exc:
            raise DownloadError(f"Could not download {url}: {exc}") from exc

        logger.info("Downloaded %.0f MB, unpacking %s", archive.stat().st_size / 1e6, SUITE_DIR)
        try:
            installed = _extract_suite(archive, games_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            raise DownloadError(f"Could not unpack {url}: {exc}") from exc

    if not installed:
        raise DownloadError(f"No {SUITE_DIR} games found in {url}")

    logger.info("Installed %d games in %s", installed, games_dir)
    return installed


def _extract_suite(archive: Path, games_dir: Path) -> int:
    """Copy out the suite's game files, and nothing else.

    Only the basename of each entry is used, so a crafted archive cannot write
    outside *games_dir*.
    """
    installed = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = Path(info.filename)
            if info.is_dir() or name.parent.name != SUITE_DIR:
                continue
            if name.suffix.lower() not in GAME_SUFFIXES:
                continue
            with zf.open(info) as source, (games_dir / name.name).open("wb") as target:
                shutil.copyfileobj(source, target)
            installed += 1
    return installed
