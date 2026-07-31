#!/usr/bin/env python3
"""Speak a session's player transmissions in the operator's voice.

The radio client writes what the player said into ``transcript.jsonl``. This
reads back every transmission of theirs and has ElevenLabs say it in the
operator's voice, writing each one beside the recordings it belongs with as
``audio/operator_<message_id>.mp3``.

    uv run make_operator_audio.py <session-id>

Existing files are left alone unless --force is given, so it is safe to run
again after a session has grown.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from elevenlabs.client import ElevenLabs

from cabin_fever_x86.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from cabin_fever_x86.sessions import PYGAME_CLIENT_COMPONENT, session_dir

#OPERATOR_VOICE_ID = "jWck2UHCtDKdhIRlMgaR"
OPERATOR_VOICE_ID = "eqz5FuihuZwmJPuvZ65E"
TTS_MODEL = "eleven_v3"
OUTPUT_FORMAT = "mp3_44100_128"
AUDIO_DIR = "audio"
TRANSCRIPT_FILE = "transcript.jsonl"
PLAYER = "user"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session_id", type=UUID, help="The session to re-voice.")
    parser.add_argument(
        "--config",
        default=None,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--voice-id",
        default=OPERATOR_VOICE_ID,
        help=f"ElevenLabs voice to speak as (default: {OPERATOR_VOICE_ID}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild clips that have already been made.",
    )
    return parser.parse_args(argv)


def read_transmissions(transcript: Path) -> list[tuple[str, str]]:
    """Every (message id, text) the player transmitted, in the order they were sent."""
    said: list[tuple[str, str]] = []
    for number, line in enumerate(transcript.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"  skip  {transcript.name} line {number}: {exc}", file=sys.stderr)
            continue
        if record.get("speaker") != PLAYER:
            continue
        message_id, text = record.get("id"), (record.get("text") or "").strip()
        if message_id and text:
            said.append((message_id, text))
    return said


def speak(client: ElevenLabs, voice_id: str, text: str) -> bytes:
    """Say one transmission in the operator's voice."""
    audio = client.text_to_speech.convert(
        text=text,
        voice_id=voice_id,
        model_id=TTS_MODEL,
        output_format=OUTPUT_FORMAT,
    )
    return b"".join(audio)


def main() -> None:
    args = _parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    client_dir = session_dir(args.session_id, PYGAME_CLIENT_COMPONENT, create=False)
    audio_dir = client_dir / AUDIO_DIR
    transcript = client_dir / TRANSCRIPT_FILE
    if not audio_dir.is_dir():
        print(f"error: no audio for session {args.session_id} at {audio_dir}", file=sys.stderr)
        raise SystemExit(1)
    if not transcript.is_file():
        print(f"error: no transcript at {transcript}", file=sys.stderr)
        raise SystemExit(1)

    said = read_transmissions(transcript)
    if not said:
        print(f"Nothing from the player in {transcript}.")
        return

    api_key = config.client.elevenlabs_api_key
    if not api_key:
        print(
            "error: no ElevenLabs API key. Set client.elevenlabs_api_key in the "
            "config, or export ELEVENLABS_API_KEY if the config reads it from there.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client = ElevenLabs(api_key=api_key)
    made = skipped = failed = 0

    for message_id, text in said:
        target = audio_dir / f"operator_{message_id}.mp3"
        if target.exists() and not args.force:
            print(f"  skip  {target.name} (already made)")
            skipped += 1
            continue

        print(f"  ...   {text[:60]}", end="", flush=True)
        try:
            target.write_bytes(speak(client, args.voice_id, text))
        except Exception as exc:  # noqa: BLE001 - one bad line must not stop the rest
            print(f"\r  fail  {message_id}: {exc}")
            failed += 1
            continue

        print(f"\r  made  {target.name} ({target.stat().st_size:,} bytes)")
        made += 1

    print(f"\n{made} made, {skipped} skipped, {failed} failed in {audio_dir}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
