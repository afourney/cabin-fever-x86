"""Session discovery and activity ordering."""

import os
from uuid import uuid4

from cabin_fever_x86_core.sessions import SERVER_COMPONENT, find_sessions, session_dir


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
