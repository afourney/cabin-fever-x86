"""Speech to text and back, for whichever client is carrying the words.

Nothing here knows how the audio was captured or how it will be played, so any
client can share it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from io import BytesIO

from elevenlabs.client import AsyncElevenLabs, ElevenLabs

STT_MODEL = "scribe_v2"
STT_LANGUAGE = "eng"  # None lets the model auto-detect.

TTS_MODEL = "eleven_v3"
# TTS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # George
# TTS_VOICE_ID = "gGNPBRoUZm1UG9WqGVlW"  # "Sia"
TTS_VOICE_ID = "aMSt68OGf4xUZAnLpTU8"  # "Juniper"
# TTS_VOICE_ID = "OZxMHsGaBmV5pjMIDIn0"  # "Amy"
TTS_FORMAT = "mp3_44100_128"
TTS_SUFFIX = TTS_FORMAT.split("_")[0]  # what a synthesised clip should be called
PCM_SAMPLE_RATE = 24_000


class VoiceError(Exception):
    """Speech to text or text to speech did not come back."""


async def stream_speech(client: AsyncElevenLabs, text: str) -> AsyncIterator[bytes]:
    """Yield mono signed 16-bit little-endian PCM as it is generated.

    Keep the provider's format and request options here. Closing this iterator
    also closes its HTTP response, including when the listener disconnects.
    """
    try:
        async with aclosing(
            client.text_to_speech.stream(
                text=text,
                voice_id=TTS_VOICE_ID,
                model_id=TTS_MODEL,
                output_format=f"pcm_{PCM_SAMPLE_RATE}",
                request_options={"chunk_size": 2048, "max_retries": 0},
            )
        ) as audio:
            async for chunk in audio:
                if chunk:
                    yield chunk
    except Exception as exc:
        raise VoiceError(str(exc)) from exc


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
