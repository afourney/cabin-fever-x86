"""Where each side of a session keeps its data.

The server and the clients may be on different machines, so each keeps its own
tree under ``data/sessions/<session_id>/<component>/``. They share only the
session id, which is enough to line the logs up afterwards.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cabin_fever_x86.messages import SessionInfo

DEFAULT_DATA_ROOT = Path("data")

# The component directory each program writes under.
SERVER_COMPONENT = "server"
TEXT_CLIENT_COMPONENT = "text_client"
WEB_CLIENT_COMPONENT = "web_client"


def session_dir(
    session_id: UUID | str,
    component: str,
    root: str | os.PathLike[str] | None = None,
    create: bool = True,
) -> Path:
    """Return ``<root>/sessions/<session_id>/<component>``, creating it by default."""
    path = Path(root or DEFAULT_DATA_ROOT) / "sessions" / str(session_id) / component
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def session_exists(
    session_id: UUID | str,
    component: str,
    root: str | os.PathLike[str] | None = None,
) -> bool:
    """Whether *component* already has a directory for this session."""
    return session_dir(session_id, component, root, create=False).is_dir()


def find_sessions(
    component: str,
    root: str | os.PathLike[str] | None = None,
) -> list[SessionInfo]:
    """List the sessions that have a *component* directory, most recent first.

    Directories whose names are not session ids are ignored, so unrelated
    clutter under ``data/sessions/`` cannot break a listing.
    """
    sessions_root = Path(root or DEFAULT_DATA_ROOT) / "sessions"
    if not sessions_root.is_dir():
        return []

    found: list[SessionInfo] = []
    for entry in sessions_root.iterdir():
        component_dir = entry / component
        if not component_dir.is_dir():
            continue
        try:
            session_id = UUID(entry.name)
        except ValueError:
            continue
        modified = datetime.fromtimestamp(component_dir.stat().st_mtime, tz=UTC)
        found.append(SessionInfo(session_id=session_id, modified=modified))

    found.sort(key=lambda info: info.modified, reverse=True)
    return found
