#!/usr/bin/env python3
"""Stitch a session back together as one recording of the conversation.

Walks ``transcript.jsonl`` in order and collects the clip for each turn: the
operator's voice for what the player transmitted (``operator_<id>.*``, made by
make_operator_audio.py), and whatever clip the transcript points at for the
replies (``clean_<id>.mp3``, as synthesised). They are joined with a beat of
silence between turns and written out as one file.

The replies come out dry. The web client applies its radio treatment in the
browser on the way to the speakers, so what a listener heard is never written
to disk and cannot be recovered from a session.

    uv run narrate_replay.py <session-id>
    uv run narrate_replay.py <session-id> --play

Needs ffmpeg on the PATH: every clip is decoded through it, so mp3 and wav at
different rates end up in the same shape before they are joined.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import numpy as np
import soundfile as sf

from cabin_fever_x86_core.sessions import WEB_CLIENT_COMPONENT, session_dir

AUDIO_DIR = "audio"
TRANSCRIPT_FILE = "transcript.jsonl"
REPLAY_FILE = "replay.wav"
PLAYER = "user"

# The pause between one turn and the next, in seconds.
MIN_GAP = 0.7
MAX_GAP = 1.5

SAMPLE_RATE = 48_000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session_id", type=UUID, help="The session to replay.")
    parser.add_argument(
        "--out",
        default=None,
        help=f"Where to write the replay (default: the session's {REPLAY_FILE}).",
    )
    parser.add_argument(
        "--min-gap",
        type=float,
        default=MIN_GAP,
        help=f"Shortest pause between turns, in seconds (default: {MIN_GAP}).",
    )
    parser.add_argument(
        "--max-gap",
        type=float,
        default=MAX_GAP,
        help=f"Longest pause between turns, in seconds (default: {MAX_GAP}).",
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Play the replay once it has been written.",
    )
    return parser.parse_args(argv)


def turns(transcript: Path, audio_dir: Path) -> list[tuple[str, Path]]:
    """The clip for each turn, in the order it happened.

    The player's own recording is swapped for the operator's voice; the
    companion is heard exactly as the radio delivered it.
    """
    found: list[tuple[str, Path]] = []

    for number, line in enumerate(transcript.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"  skip  line {number}: {exc}", file=sys.stderr)
            continue

        speaker, clip = record.get("speaker"), record.get("audio")
        if not clip:
            continue  # session notes, and anything that never got a voice

        if speaker == PLAYER:
            # player_<id>.wav -> operator_<id>.*, whatever it was written as
            stem = Path(clip).stem.replace("player_", "operator_", 1)
            matches = sorted(audio_dir.glob(f"{stem}.*"))
            if not matches:
                print(f"  skip  {stem}: not made yet", file=sys.stderr)
                continue
            found.append((speaker, matches[0]))
            continue

        path = transcript.parent / clip
        if not path.is_file():
            print(f"  skip  {clip}: missing", file=sys.stderr)
            continue
        found.append((speaker, path))

    return found


def decode(path: Path) -> np.ndarray:
    """Read one clip as mono at the replay's sample rate."""
    wav = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "wav", "-"],
        capture_output=True,
        check=True,
    ).stdout
    audio, _rate = sf.read(io.BytesIO(wav), dtype="float32")
    return audio


def main() -> None:
    args = _parse_args()
    if args.min_gap < 0 or args.max_gap < args.min_gap:
        print("error: --min-gap must be >= 0 and no larger than --max-gap", file=sys.stderr)
        raise SystemExit(1)

    client_dir = session_dir(args.session_id, WEB_CLIENT_COMPONENT, create=False)
    transcript = client_dir / TRANSCRIPT_FILE
    audio_dir = client_dir / AUDIO_DIR
    if not transcript.is_file():
        print(f"error: no transcript at {transcript}", file=sys.stderr)
        raise SystemExit(1)

    found = turns(transcript, audio_dir)
    if not found:
        print(
            f"error: nothing to play from {transcript}. Run make_operator_audio.py "
            "first if the operator clips have not been made.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    pieces: list[np.ndarray] = []
    seconds = 0.0
    for index, (speaker, path) in enumerate(found):
        if index:  # a beat between turns, never before the first
            gap = random.uniform(args.min_gap, args.max_gap)
            pieces.append(np.zeros(int(gap * SAMPLE_RATE), dtype="float32"))
            seconds += gap
        try:
            audio = decode(path)
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            print(f"  skip  {path.name}: {exc}", file=sys.stderr)
            continue
        pieces.append(audio)
        seconds += len(audio) / SAMPLE_RATE
        print(f"  {speaker:9} {path.name}  ({len(audio) / SAMPLE_RATE:5.1f}s)")

    out = Path(args.out) if args.out else client_dir / REPLAY_FILE
    sf.write(out, np.concatenate(pieces), SAMPLE_RATE)
    print(f"\n{len(found)} turns, {seconds / 60:.1f} minutes -> {out}")

    if args.play:
        subprocess.run(["ffplay", "-v", "error", "-nodisp", "-autoexit", str(out)], check=False)


if __name__ == "__main__":
    main()
