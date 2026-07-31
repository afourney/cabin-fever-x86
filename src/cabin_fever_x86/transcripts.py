"""A client's record of one session: what was said, and the audio of it."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from cabin_fever_x86.sessions import session_dir

TRANSCRIPT_FILE = "transcript.jsonl"
AUDIO_DIR = "audio"


class Transcript:
    """One JSON object per transmission, with the clips beside it.

    Every clip that crosses the channel is kept, named after the message it
    belongs to::

        transcript.jsonl
        audio/player_<message_id>.<ext>   what the player transmitted
        audio/clean_<message_id>.mp3      the reply as synthesised

    Each record points at the clip the listener actually heard.
    """

    def __init__(self, session_id: UUID, component: str) -> None:
        """Open the record for one session, creating its folders."""
        self.dir = session_dir(session_id, component)
        self.path = self.dir / TRANSCRIPT_FILE
        self.audio_dir = self.dir / AUDIO_DIR
        self.audio_dir.mkdir(parents=True, exist_ok=True)

    def save_audio(self, kind: str, message_id: UUID, data: bytes, suffix: str) -> str:
        """Write one clip and return its path, relative to the transcript."""
        path = self.audio_dir / f"{kind}_{message_id}.{suffix}"
        path.write_bytes(data)
        return str(path.relative_to(self.dir))

    def log(
        self,
        speaker: str,
        message_id: UUID | None,
        text: str,
        audio: str | None = None,
    ) -> None:
        """Append one transmission to the transcript."""
        record = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "speaker": speaker,
            "id": str(message_id) if message_id is not None else None,
            "text": text,
            "audio": audio,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
