"""Best-effort checks for newer launcher releases on PyPI."""

from __future__ import annotations

import contextlib
import json
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from packaging.version import InvalidVersion, Version

PACKAGE_NAME = "cabin-fever-x86"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
CACHE_NAME = "launcher-pypi-version-cache.json"
CACHE_MAX_AGE = 24 * 60 * 60
REQUEST_TIMEOUT = 2


def _cached_version(cache: Path, now: float) -> str | None:
    """Return a fresh cached version, or ``None`` when it cannot be used."""
    try:
        payload = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(payload["checked_at"]) >= CACHE_MAX_AGE:
            return None
        version = payload["version"]
        Version(version)
        return version
    except (OSError, ValueError, TypeError, KeyError, InvalidVersion):
        return None


def _pypi_version() -> str | None:
    """Fetch the latest release version from PyPI without raising."""
    request = urllib.request.Request(
        PYPI_URL,
        headers={"User-Agent": f"{PACKAGE_NAME} version check"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            version = json.load(response)["info"]["version"]
        Version(version)
        return version
    except (OSError, ValueError, TypeError, KeyError, InvalidVersion):
        return None


def latest_version(home: Path, now: float | None = None) -> str | None:
    """Return PyPI's latest version, using a daily cache when possible."""
    checked_at = time.time() if now is None else now
    cache = home / CACHE_NAME
    if cached := _cached_version(cache, checked_at):
        return cached

    latest = _pypi_version()
    if latest is not None:
        with contextlib.suppress(OSError):
            cache.write_text(
                json.dumps({"checked_at": checked_at, "version": latest}) + "\n",
                encoding="utf-8",
            )
    return latest


def _installer_name() -> str | None:
    """Return the tool recorded as installing this distribution, if any."""
    try:
        installer = distribution(PACKAGE_NAME).read_text("INSTALLER")
    except (PackageNotFoundError, OSError):
        return None
    return installer.strip().lower() if installer else None


def print_upgrade_notice(home: Path, installed: str | None) -> None:
    """Print upgrade guidance when PyPI has a newer launcher release."""
    if installed is None:
        return

    latest = latest_version(home)
    try:
        update_available = latest is not None and Version(latest) > Version(installed)
    except InvalidVersion:
        return
    if not update_available:
        return

    print(f"A newer launcher is available: {latest}")
    if _installer_name() == "uv":
        print("If installed as a uv tool:")
        print(f"  uv tool upgrade {PACKAGE_NAME}")
        print("If installed in a virtual environment:")
        print(f"  uv pip install --upgrade {PACKAGE_NAME}")
    else:
        print("Upgrade with:")
        print(f"  python -m pip install --upgrade {PACKAGE_NAME}")
    print("")
