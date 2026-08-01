"""Cabin Fever x86: a shared game night with a companion over a simulated radio."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version("cabin-fever-x86-core")
except PackageNotFoundError:
    __version__ = None

del PackageNotFoundError, _version
