"""The radio hardware: the microphone, the speaker, and what happens in between.

Adapted from the prototype in ``~/personal/tmp/game.py``. Everything here is
synchronous and blocking on purpose — the client drives it from asyncio and
pushes the slow parts onto threads.
"""

from __future__ import annotations

import logging
import os
import queue
import random
import subprocess
import tempfile
from collections import deque
from io import BytesIO
from pathlib import Path

import numpy as np
import pygame
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 1

MIXER_RATE = 44_100

# Puts a clean clip over the airwaves: static, squelch, the lot.
RADIO_SCRIPT = Path(__file__).with_name("radio.bash")


def over_the_air(audio: bytes) -> bytes:
    """Run a clip through ``radio.bash``, falling back to the dry clip.

    The script works on paths and shells out to ffmpeg, so each clip gets its
    own temp directory and its own seed — the static never repeats.
    """
    if not RADIO_SCRIPT.exists():
        return audio

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "reply.mp3"
        processed = Path(tmp) / "reply_radio.wav"
        source.write_bytes(audio)

        try:
            subprocess.run(
                ["bash", str(RADIO_SCRIPT), str(source), str(processed)],
                env={**os.environ, "SEED": str(random.randint(1, 1_000_000))},
                capture_output=True,
                check=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            logger.warning(
                "Radio effect failed, playing dry: %s %s",
                exc,
                detail.decode(errors="replace").strip(),
            )
            return audio

        return processed.read_bytes()


def describe_output() -> str:
    """Describe what the mixer opened, for the log. Silence has more than one cause."""
    init = pygame.mixer.get_init()
    if init is None:
        return "mixer not initialised — nothing will be heard"

    frequency, _size, channels = init
    detail = f"{frequency} Hz, {channels} channel(s)"
    try:
        from pygame._sdl2 import audio as sdl2_audio

        devices = sdl2_audio.get_audio_device_names(False)
    except (ImportError, pygame.error):  # optional, and platform-dependent
        return detail
    return f"{detail}, devices: {', '.join(devices) or 'none'}"


class RadioPlayer:
    """Plays replies one at a time, and never while the mic is hot.

    Overlapping clips would sound like two people talking over each other, and
    playing through the speakers during a transmission would feed the reply
    straight back into the next recording.
    """

    def __init__(self) -> None:
        self.queued: deque[pygame.mixer.Sound] = deque()
        self.channel = pygame.mixer.Channel(0)

    def enqueue(self, audio: bytes) -> None:
        self.queued.append(pygame.mixer.Sound(BytesIO(audio)))

    def update(self, mic_is_hot: bool) -> None:
        if mic_is_hot or not self.queued or self.channel.get_busy():
            return
        sound = self.queued.popleft()
        # Worth a line: when nothing is heard, this says whether the fault is
        # upstream of the speakers or downstream of them.
        logger.info("Playing %.1fs at volume %.2f", sound.get_length(), sound.get_volume())
        self.channel.play(sound)

    @property
    def busy(self) -> bool:
        return bool(self.queued) or self.channel.get_busy()

    def interrupt(self) -> None:
        """Cut the clip that is playing; anything queued still gets its turn."""
        self.channel.stop()

    def stop(self) -> None:
        self.queued.clear()
        self.channel.stop()


class PushToTalkRecorder:
    """Holds the key, holds the mic."""

    def __init__(self) -> None:
        self.recording = False
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self.chunks: list[np.ndarray] = []
        self.take = 0
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            callback=self._audio_callback,
        )

    def _audio_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: object,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.warning("Audio: %s", status)
        # The audio callback should do very little work.
        if self.recording:
            self.audio_queue.put(indata.copy())

    def start(self) -> None:
        if self.recording:
            return
        # Discard anything left from an earlier recording.
        self._drain_queue()
        self.chunks.clear()
        self.recording = True

    def stop(self) -> tuple[int, bytes] | None:
        """End the take and return (take number, encoded WAV bytes)."""
        if not self.recording:
            return None

        self.recording = False
        self._drain_queue()
        if not self.chunks:
            logger.info("No audio captured")
            return None

        audio = np.concatenate(self.chunks, axis=0)

        # Encode in memory: the bytes are what gets transcribed, so nothing on
        # disk can corrupt a request that is still in flight.
        buffer = BytesIO()
        sf.write(buffer, audio, SAMPLE_RATE, format="WAV")

        self.take += 1
        logger.info("Take %d: %.2fs", self.take, len(audio) / SAMPLE_RATE)
        return self.take, buffer.getvalue()

    def update(self) -> None:
        """Move captured chunks out of the callback queue."""
        self._drain_queue()

    def close(self) -> None:
        if self.recording:
            self.stop()
        self.stream.stop()
        self.stream.close()

    def _drain_queue(self) -> None:
        while True:
            try:
                self.chunks.append(self.audio_queue.get_nowait())
            except queue.Empty:
                return
