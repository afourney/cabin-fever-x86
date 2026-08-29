"""The session's ledger of what each request to the model cost.

One line per request, written beside the conversation it was spent on. Nothing
here prices anything: rates change, and change retroactively relative to a log,
so the file keeps what the API reported and leaves the arithmetic to whoever
reads it back.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

#: Bumped when the shape of a record changes. The file is only ever appended
#: to, so old lines stay in the shape they were written in.
SCHEMA_VERSION = 1

# What the request was serving. A player transmission or an interruption is a
# turn in its own right; a hint or a compaction is work done on behalf of one,
# and says so with ``parent_turn_id``.
PLAYER_TURN = "player"
HINT_TURN = "hint"
COMPACTION_TURN = "compaction"

#: Handed the response behind one request, so its cost reaches the ledger from
#: code that has no business knowing where the ledger is.
UsageCallback = Callable[[Any], None]


def usage_dict(usage: Any) -> dict[str, Any]:
    """Return the token breakdown as plain JSON, whole rather than picked apart.

    The API grows new counters — cache writes were one — and a ledger that only
    knows today's fields cannot be read back for them later.
    """
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        return dict(dump())

    # Anything that is not a pydantic model is read field by field, so a
    # stand-in that carries only part of the breakdown still lands as one.
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "input_tokens_details": {
            "cached_tokens": getattr(input_details, "cached_tokens", 0),
            "cache_write_tokens": getattr(input_details, "cache_write_tokens", 0),
        },
        "output_tokens": getattr(usage, "output_tokens", 0),
        "output_tokens_details": {
            "reasoning_tokens": getattr(output_details, "reasoning_tokens", 0),
        },
        "total_tokens": getattr(usage, "total_tokens", 0),
    }


class UsageLog:
    """Every request this session made, one JSON object per line.

    Unlike ``messages.jsonl``, this is never rotated or rewritten. The
    conversation is compacted to keep the next request affordable; the record of
    what the earlier ones cost is not part of that bargain.
    """

    def __init__(self, path: Path, session_id: UUID) -> None:
        """Open the ledger for one session. The file is created on first write."""
        self.path = path
        self._session_id = session_id

    def record(
        self,
        response: Any,
        *,
        turn_type: str,
        turn_id: UUID,
        parent_turn_id: UUID | None = None,
        call_id: str | None = None,
        model_round: int = 0,
    ) -> None:
        """Append what one response cost.

        A response that came back without a usage breakdown is not written down:
        there is nothing to account for, and a row of zeroes would read as a
        request that was free rather than one that was never measured.
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return

        reasoning = getattr(response, "reasoning", None)
        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "session_id": str(self._session_id),
            "turn_type": turn_type,
            "turn_id": str(turn_id),
            "parent_turn_id": str(parent_turn_id) if parent_turn_id is not None else None,
            "call_id": call_id,
            "model_round": model_round,
            "response_id": getattr(response, "id", None),
            "response_created_at": getattr(response, "created_at", None),
            # What the request was actually served by, rather than what was
            # asked for: the model can resolve to a dated build, and the tier
            # can differ from the one requested. Both decide the price.
            "model": getattr(response, "model", None),
            "service_tier": getattr(response, "service_tier", None),
            "prompt_cache_key": getattr(response, "prompt_cache_key", None),
            # How long the prefix was allowed to stay cached, as the provider
            # settled it rather than as it was asked for: a miss reads very
            # differently under a policy that expires in minutes.
            "prompt_cache_retention": getattr(response, "prompt_cache_retention", None),
            "reasoning": {
                "effort": getattr(reasoning, "effort", None),
                "context": getattr(reasoning, "context", None),
            },
            "usage": usage_dict(usage),
        }

        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError as exc:
            # Losing a line of accounting is not worth dropping the night over.
            logger.warning("Could not write to %s: %s", self.path, exc)
