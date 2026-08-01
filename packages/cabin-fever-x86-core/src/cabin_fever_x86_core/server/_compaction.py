"""Starting the night over from notes, when the conversation gets too long.

A model has a context limit, and a session that runs all night will find it.
Rather than let a request fail, the companion steps away from the radio for a
moment, writes down everything worth keeping, and comes back working from those
notes instead of the whole transcript.

The excuse matters as much as the summary. The player is told the cat knocked
something over, hears a pause, and is told the vase survived — and on the far
side of it the companion still knows who they are and where the game got to,
because the notes came with it. Nothing is explained, because from the player's
side nothing happened.

Only the notes cost a request. Everything either side of them — the excuse, the
stage directions framing the notes, the return — is written down here in
advance::

    assistant  "Hang on — the cat just knocked something off the shelf."
    user       <stage_direction>… your notes: {summary}</stage_direction>
    assistant  "False alarm. The vase lives to see another day."
    user       <stage_direction>Continue where you left off.</stage_direction>
    …          whatever the model was in the middle of saying

That last part is what triggered the compaction, kept exactly as it came back so
a tool call it had already decided on still gets its answer.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXCUSES_FILE = Path(__file__).with_name("compaction_excuses.jsonl")

# The tag the companion has seen every direction arrive in all session, so the
# notes do not show up wearing something unfamiliar. Kept in step with
# _game.STAGE_DIRECTION by the tests rather than imported, since importing would
# be a cycle.
STAGE_DIRECTION = "stage_direction"

# What the companion is asked to write down. Deliberately about picking the
# night back up rather than about summarising: the model is playing someone with
# a notebook, not compressing a transcript.
COMPACTION_PROMPT = (
    "You have to step away from the radio and the desk — you do not know for how "
    "long. Could be minutes, could be longer. You fully intend to pick this up "
    "again: the same conversation, the same game, the same person on the other "
    "end.\n\n"
    "So write it all down first, while it is in front of you. Everything you would "
    "be annoyed to have lost:\n"
    "- Who you are talking to. What you have learned about them, what they have "
    "told you about themselves, how the two of you have been getting on, anything "
    "you have promised or agreed.\n"
    "- What has been said. The threads still open, the jokes that landed, whatever "
    "you were in the middle of when you had to go.\n"
    "- The game. Which one is running, where it stands, what you can see on the "
    "screen. Score and moves if you know them. Which save you last wrote.\n"
    "- The map as you understand it. Rooms, exits, what is where, what is carried.\n"
    "- What you have tried. Especially what did not work, so you do not spend the "
    "rest of the night doing it again. Puzzles that are still open, and what you "
    "suspect about them.\n"
    "- What you meant to do next, and why.\n\n"
    "Write it as notes to yourself — dense, specific, in whatever order makes "
    "sense. Names, numbers and exact wording where they matter. Length is not a "
    "problem; leaving something out is. This is not a transmission and nobody else "
    "will read it, so do not address the operator and do not call any tools. Just "
    "the notes."
)

# How the notes are handed back on the other side.
NOTES_DIRECTION = (
    "You have been talking with the operator all night, and playing as you went, "
    "and you have been keeping careful notes. You have just sat back down. Read "
    "them and carry on as though you never got up — the operator does not know you "
    "were writing anything and must not be told. Do not summarise your notes to "
    "them, do not mention stepping away beyond whatever you already said, and do "
    "not start the conversation over.\n\nYour notes:\n{summary}"
)

RESUME_DIRECTION = "Continue where you left off."


class CompactionError(Exception):
    """The night could not be written down."""


@dataclass(frozen=True)
class Excuse:
    """A reason to put the handset down, and what is said on picking it back up."""

    away: str
    back: str


def load_excuses(path: Path = EXCUSES_FILE) -> list[Excuse]:
    """Read the excuses, skipping anything unreadable rather than failing.

    A missing or malformed file leaves the list short or empty, which the caller
    treats as a reason to compact quietly rather than a reason to stop: notes are
    worth having even without a story to explain the pause.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("No excuses to read from %s: %s", path, exc)
        return []

    found: list[Excuse] = []
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            record = json.loads(line)
            found.append(Excuse(away=str(record["away"]), back=str(record["back"])))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Skipping %s line %d: %s", path.name, number, exc)
    return found


def draw_excuse(excuses: list[Excuse]) -> Excuse | None:
    """Pick one at random, or nothing if there are none to pick."""
    return random.choice(excuses) if excuses else None


def direction(text: str) -> dict[str, Any]:
    """Wrap *text* as a direction, in the shape the conversation is carried in."""
    return {"role": "user", "content": f"<{STAGE_DIRECTION}>{text}</{STAGE_DIRECTION}>"}


def notes_request(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put the request for notes on the end of the conversation to summarise."""
    return [*history, direction(COMPACTION_PROMPT)]


def rebuilt(
    summary: str,
    excuse: Excuse | None,
    tail: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the conversation the companion carries on from.

    *tail* is whatever came back from the request that tripped the threshold,
    kept exactly as it was: it may hold a tool call that has not been answered
    yet, and dropping it would leave the model waiting on itself.
    """
    rebuilt: list[dict[str, Any]] = []
    if excuse is not None:
        rebuilt.append({"role": "assistant", "content": excuse.away})
    rebuilt.append(direction(NOTES_DIRECTION.format(summary=summary.strip())))
    if excuse is not None:
        rebuilt.append({"role": "assistant", "content": excuse.back})
    rebuilt.append(direction(RESUME_DIRECTION))
    rebuilt.extend(tail)
    return rebuilt


def rotate(journal: Path) -> Path | None:
    """Move *journal* aside, to the first ``messages.NNNN.jsonl`` going spare.

    Returns where it went, or ``None`` if there was nothing there to move. The
    old transcript is kept rather than dropped: the summary is a lossy thing and
    the night it came from is worth being able to read back.
    """
    if not journal.exists():
        return None

    for number in range(1, 10_000):
        aside = journal.with_name(f"{journal.stem}.{number:04d}{journal.suffix}")
        if not aside.exists():
            journal.rename(aside)
            return aside
    raise CompactionError(f"nowhere left to move {journal.name} aside to")


def rewrite(journal: Path, messages: list[dict[str, Any]]) -> None:
    """Write *messages* out as the whole journal, one item per line."""
    with journal.open("w", encoding="utf-8") as handle:
        for item in messages:
            handle.write(json.dumps(item) + "\n")
