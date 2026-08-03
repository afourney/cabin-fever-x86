"""Speech to text and back, for whichever client is carrying the words.

Nothing here knows how the audio was captured or how it will be played, so any
client can share it.
"""

from __future__ import annotations

from io import BytesIO

from elevenlabs.client import ElevenLabs

STT_MODEL = "scribe_v2"
STT_LANGUAGE = "eng"  # None lets the model auto-detect.

TTS_MODEL = "eleven_v3"
# TTS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George
# TTS_VOICE_ID = "gGNPBRoUZm1UG9WqGVlW"  # "Sia"
TTS_VOICE_ID = "aMSt68OGf4xUZAnLpTU8"  # "Juniper"
# TTS_VOICE_ID = "OZxMHsGaBmV5pjMIDIn0"  # "Amy"
TTS_FORMAT = "mp3_44100_128"
TTS_SUFFIX = TTS_FORMAT.split("_")[0]  # what a synthesised clip should be called


class VoiceError(Exception):
    """Speech to text or text to speech did not come back."""


def transcribe(
    client: ElevenLabs,
    audio: bytes,
    filename: str = "take.wav",
    mimetype: str = "audio/wav",
) -> str:
    """Turn one recorded take into text.

    *filename* and *mimetype* describe what was captured — a wav from a
    microphone stream, or whatever the browser's MediaRecorder produced.
    """
    try:
        response = client.speech_to_text.convert(
            # A fresh buffer per attempt: the SDK consumes the stream.
            file=(filename, BytesIO(audio), mimetype),
            model_id=STT_MODEL,
            tag_audio_events=True,
            language_code=STT_LANGUAGE,
            diarize=True,
        )
    except Exception as exc:
        raise VoiceError(str(exc)) from exc
    return response.text or ""


def synthesize(
    client: ElevenLabs,
    text: str,
    voice_id: str = TTS_VOICE_ID,
    output_format: str = TTS_FORMAT,
) -> bytes:
    """Read a line back in the companion's voice."""
    try:
        # convert() streams chunks; collect them into one clip.
        audio = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=TTS_MODEL,
            output_format=output_format,
        )
        return b"".join(audio)
    except Exception as exc:
        raise VoiceError(str(exc)) from exc
