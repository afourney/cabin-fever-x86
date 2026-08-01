"""Cabin Fever x86: Launcher Application for Windows and macOS."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("cabin-fever-x86")
except PackageNotFoundError:
    __version__ = None

del PackageNotFoundError, _version
