"""Session discovery and activity ordering."""

import os
from uuid import uuid4

import pytest

from cabin_fever_x86_core.sessions import (
    SERVER_COMPONENT,
    TEXT_CLIENT_COMPONENT,
    WEB_CLIENT_COMPONENT,
    find_sessions,
    session_dir,
    session_exists,
)


def test_server_sessions_are_ordered_by_the_messages_journal(tmp_path):
    older = uuid4()
    newer = uuid4()
    older_dir = session_dir(older, SERVER_COMPONENT, tmp_path)
    newer_dir = session_dir(newer, SERVER_COMPONENT, tmp_path)
    older_messages = older_dir / "messages.jsonl"
    newer_messages = newer_dir / "messages.jsonl"
    older_messages.touch()
    newer_messages.touch()

    os.utime(older_messages, (10, 10))
    os.utime(newer_messages, (20, 20))
    # Deliberately make the directory order disagree with journal activity.
    os.utime(older_dir, (30, 30))
    os.utime(newer_dir, (5, 5))

    assert [info.session_id for info in find_sessions(SERVER_COMPONENT, tmp_path)] == [
        newer,
        older,
    ]


def test_server_paths_and_discovery_are_scoped_to_users(tmp_path):
    session_id = uuid4()
    path = session_dir(session_id, SERVER_COMPONENT, tmp_path, user_id="alice")
    assert path == tmp_path / "users/alice/sessions" / str(session_id) / "server"
    assert session_exists(session_id, SERVER_COMPONENT, tmp_path, user_id="alice")
    for user_id in ("bob", "guest"):
        assert not session_exists(session_id, SERVER_COMPONENT, tmp_path, user_id=user_id)
        assert find_sessions(SERVER_COMPONENT, tmp_path, user_id=user_id) == []
        assert not (tmp_path / "users" / user_id).exists()


@pytest.mark.parametrize("component", [TEXT_CLIENT_COMPONENT, WEB_CLIENT_COMPONENT])
def test_client_storage_paths_are_unchanged(tmp_path, component):
    session_id = uuid4()
    path = session_dir(session_id, component, tmp_path)
    assert path == tmp_path / "sessions" / str(session_id) / component
    assert [s.session_id for s in find_sessions(component, tmp_path)] == [session_id]


@pytest.mark.parametrize("user_id", ["../alice", "/alice", "", "Alice", "a" * 65])
def test_storage_helpers_reject_invalid_user_ids(tmp_path, user_id):
    with pytest.raises(ValueError, match="user ID"):
        session_dir(uuid4(), SERVER_COMPONENT, tmp_path, user_id=user_id)
    assert list(tmp_path.iterdir()) == []
