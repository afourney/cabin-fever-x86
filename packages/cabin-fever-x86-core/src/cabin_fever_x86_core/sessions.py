"""Where each side of a session keeps its data.

The server stores sessions under ``data/users/<user_id>/sessions/<session_id>/server/``.
Clients still use ``data/sessions/<session_id>/<component>/``. They may live on
different machines; the session id lines their logs up afterwards.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cabin_fever_x86_core.messages import SessionInfo

DEFAULT_DATA_ROOT = Path("data")
GUEST_USER_ID = "guest"
_USER_ID = re.compile(r"[a-z0-9_-]{1,64}")

# The component directory each program writes under.
SERVER_COMPONENT = "server"
TEXT_CLIENT_COMPONENT = "text_client"
WEB_CLIENT_COMPONENT = "web_client"

# The server's conversation journal is the authoritative indication of when a
# session was last active.  Directory mtimes can also change for housekeeping
# unrelated to play.
MESSAGES_FILE = "messages.jsonl"

# What every request to the model cost, beside the conversation it was spent
# on. Compaction rotates and rewrites the journal; this file is only ever
# appended to, because what was spent stays spent however the context is later
# cut down.
USAGE_FILE = "usage.jsonl"


def validate_user_id(user_id: str) -> str:
    """Require a short, stable identifier safe to use as a directory name."""
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("user ID must be 1-64 lowercase letters, digits, underscores, or hyphens")
    return user_id


def _sessions_root(component: str, root: str | os.PathLike[str] | None, user_id: str) -> Path:
    base = Path(root or DEFAULT_DATA_ROOT)
    if component == SERVER_COMPONENT:
        base = base / "users" / validate_user_id(user_id)
    return base / "sessions"


def session_dir(
    session_id: UUID | str,
    component: str,
    root: str | os.PathLike[str] | None = None,
    create: bool = True,
    *,
    user_id: str = GUEST_USER_ID,
) -> Path:
    """Return a component's session directory, scoped to a user on the server."""
    path = _sessions_root(component, root, user_id) / str(session_id) / component
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def session_exists(
    session_id: UUID | str,
    component: str,
    root: str | os.PathLike[str] | None = None,
    *,
    user_id: str = GUEST_USER_ID,
) -> bool:
    """Whether *component* already has a directory for this session."""
    return session_dir(session_id, component, root, create=False, user_id=user_id).is_dir()


def find_sessions(
    component: str,
    root: str | os.PathLike[str] | None = None,
    *,
    user_id: str = GUEST_USER_ID,
) -> list[SessionInfo]:
    """List the sessions that have a *component* directory, most recent first.

    Directories whose names are not session ids are ignored, so unrelated
    clutter cannot break a listing. Server listings include only *user_id*.
    """
    sessions_root = _sessions_root(component, root, user_id)
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
        activity_path = (
            component_dir / MESSAGES_FILE if component == SERVER_COMPONENT else component_dir
        )
        # A newly-created server session can briefly exist before its first
        # message is journalled. Keep it listable during that window.
        if not activity_path.exists():
            activity_path = component_dir
        modified = datetime.fromtimestamp(activity_path.stat().st_mtime, tz=UTC)
        found.append(SessionInfo(session_id=session_id, modified=modified))

    found.sort(key=lambda info: info.modified, reverse=True)
    return found
