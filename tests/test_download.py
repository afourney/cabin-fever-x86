"""The game download, and the guard that keeps it out of CI.

Nothing here touches the network: the one test that unpacks anything builds
its own archive first.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from cabin_fever_x86.server._download import (
    NO_DOWNLOAD_VARS,
    SUITE_DIR,
    downloads_blocked,
    ensure_games,
    has_games,
)

# Somewhere a download would certainly fail, so an attempted one cannot pass
# quietly as a success.
NOWHERE = "file:///nonexistent/z-machine-games.zip"


@pytest.fixture(autouse=True)
def _no_inherited_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test from a known environment, whatever the runner set."""
    for name in NO_DOWNLOAD_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("name", NO_DOWNLOAD_VARS)
def test_nothing_is_downloaded_when_the_flag_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "true")
    assert downloads_blocked() == name
    # Would raise DownloadError if it went anywhere near the network.
    assert ensure_games(tmp_path / "games", NOWHERE) == 0


def test_the_flag_can_be_turned_off_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "false")
    assert downloads_blocked() is None


def test_games_already_on_disk_are_left_alone(tmp_path: Path) -> None:
    games = tmp_path / "games"
    games.mkdir()
    (games / "zork1.z5").write_bytes(b"not really a game")

    assert has_games(games)
    assert ensure_games(games, NOWHERE) == 0


def test_only_the_suite_is_unpacked(tmp_path: Path) -> None:
    archive = tmp_path / "suite.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"repo-main/{SUITE_DIR}/zork1.z5", b"game")
        zf.writestr(f"repo-main/{SUITE_DIR}/advent.z3", b"game")
        zf.writestr(f"repo-main/{SUITE_DIR}/README.md", b"not a game")
        zf.writestr("repo-main/other-suite/hitchhiker.z5", b"wrong folder")

    games = tmp_path / "games"
    assert ensure_games(games, f"file://{archive}") == 2
    assert sorted(path.name for path in games.iterdir()) == ["advent.z3", "zork1.z5"]
