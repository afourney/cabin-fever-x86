"""The terminal the text client draws itself on.

Transmissions arrive whenever the cabin feels like sending one, which is more
than ``input()`` can survive: the line you were halfway through typing gets
walked on by whatever Sam just said. So the terminal is driven directly
instead. The bottom of the screen is a block this module owns — a status line
and a prompt — and everything else scrolls past above it. A line arriving
while you type erases the block, prints itself, and puts the block back with
your half-finished sentence and the cursor exactly where they were.

The colours are the web client's stylesheet, read out of ``index.html``. The
two clients are the same radio and should not be different colours.

Where none of that is possible — a pipe, a recording, Windows without
``termios`` — :class:`Console` prints the same lines the plain way.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import shutil
import signal
import sys
import textwrap
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TextIO

try:  # POSIX only; everywhere else falls back to the line-buffered console.
    import termios
    import tty

    RAW_MODE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on Windows
    RAW_MODE_AVAILABLE = False

#: The log is `min(70ch, 100%)` in the browser. This is its twin.
WRAP_LIMIT = 76

#: Width of the speaker column: ``"sam > "``. Every line hangs off it.
GUTTER = 6

#: How long a second Ctrl-C stays armed for, in seconds.
INTERRUPT_GRACE = 3.0


# --- ink --------------------------------------------------------------------

#: index.html, by another name.
PALETTE: dict[str, tuple[int, int, int]] = {
    "body": (0xCF, 0xE3, 0xD4),  # what Sam says
    "user": (0x7F, 0x9E, 0xC4),  # what you said
    "gutter": (0x46, 0x58, 0x6E),  # the speaker column
    "error": (0xC9, 0x8B, 0x7A),
    "note": (0x5D, 0x73, 0x64),  # asides, status, the quiet half of everything
    "title": (0x8F, 0xB8, 0x9B),
    "accent": (0x7A, 0xC0, 0x7A),  # the green in "x86"
}


class Ink:
    """Colour, at whatever depth the terminal admits to.

    Terminals that announce truecolor get the palette as written; the rest get
    the nearest cell of the xterm-256 cube, which is close enough that nobody
    has ever noticed. Anything that is not a terminal at all gets no escape
    codes, so a redirected session stays diffable.
    """

    def __init__(self, enabled: bool = True, truecolor: bool = True) -> None:
        self._codes = {name: self._code(rgb, enabled, truecolor) for name, rgb in PALETTE.items()}
        self._reset = "\x1b[0m" if enabled else ""
        self.enabled = enabled

    @classmethod
    def detect(cls, stream: TextIO) -> Ink:
        """Read the environment's opinion on colour."""
        enabled = (
            stream.isatty()
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb"
        )
        truecolor = os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
        return cls(enabled, truecolor)

    def __call__(self, name: str, text: str) -> str:
        """Paint *text*, or hand it back untouched when colour is off."""
        code = self._codes[name]
        return f"{code}{text}{self._reset}" if code else text

    @staticmethod
    def _code(rgb: tuple[int, int, int], enabled: bool, truecolor: bool) -> str:
        if not enabled:
            return ""
        red, green, blue = rgb
        if truecolor:
            return f"\x1b[38;2;{red};{green};{blue}m"
        return f"\x1b[38;5;{_xterm256(red, green, blue)}m"


def _xterm256(red: int, green: int, blue: int) -> int:
    """Find the closest xterm-256 index to one RGB colour."""
    if red == green == blue:  # the greyscale ramp is finer than the cube
        if red < 8:
            return 16
        if red > 248:
            return 231
        return 232 + round((red - 8) / 247 * 24)
    cube = [round(value / 255 * 5) for value in (red, green, blue)]
    return 16 + 36 * cube[0] + 6 * cube[1] + cube[2]


def _spaced(word: str) -> str:
    """Letter-space a word, the way the page's ``h1`` does."""
    return " ".join(word)


def _common_prefix(words: Sequence[str]) -> str:
    """As much of *words* as all of them agree on."""
    shortest = min(words, key=len)
    for index, char in enumerate(shortest):
        if any(word[index] != char for word in words):
            return shortest[:index]
    return shortest


# --- glyphs -----------------------------------------------------------------


@dataclass(frozen=True)
class Glyphs:
    """The handful of characters the layout is drawn with."""

    tick: str  # between a speaker and what they said
    rule: str  # the lines around the title
    dot: str  # the beat in "receiving ..."
    more: str  # marks a prompt scrolled off its edge


UNICODE = Glyphs(tick="▸", rule="─", dot="·", more="…")
ASCII = Glyphs(tick=">", rule="-", dot=".", more="~")


#: Each kind of line: its label, the label's colour, and the text's.
KINDS: dict[str, tuple[str, str, str]] = {
    "assistant": ("sam", "gutter", "body"),
    "user": ("you", "gutter", "user"),
    "error": ("err", "error", "error"),
    "note": ("", "note", "note"),
}


# --- the console ------------------------------------------------------------


class Console:
    """Colour, wrapping, and the shape of a line, on a terminal taken as it is.

    This is the console for a terminal that cannot be driven, or is not one:
    the log is printed a block at a time and a prompt is left after each, so
    a person watching still gets one. A pipe gets nothing but the lines.
    :class:`LiveConsole` keeps the rendering and takes the terminal over.
    """

    def __init__(self, out: TextIO, ink: Ink, glyphs: Glyphs) -> None:
        """Draw on *out*, in *ink*, with *glyphs*."""
        self._out = out
        self.ink = ink
        self.glyphs = glyphs

    @property
    def prompt(self) -> str:
        """The prompt, which is the speaker column with your name in it."""
        return f"you {self.glyphs.tick} "

    @property
    def columns(self) -> int:
        """How wide the terminal is right now."""
        return shutil.get_terminal_size((80, 24)).columns

    @property
    def wrap_width(self) -> int:
        """How wide a line of the log is allowed to get."""
        return max(min(self.columns - 1, WRAP_LIMIT), 24)

    @property
    def _body_width(self) -> int:
        """How much of that is left once the speaker column has taken its cut."""
        return max(self.wrap_width - GUTTER, 16)

    # -- what goes on the screen --

    def say(self, kind: str, text: str) -> None:
        """Put one transmission in the log, wrapped into the speaker column."""
        self._emit("\n".join(self.format(kind, text)))

    def echo(self, text: str) -> None:
        """Put a command back in the log, where the player typed it."""
        self._emit(self.ink("note", f"{self.prompt}{text}"))

    def listing(self, rows: Sequence[str], colour: str = "note") -> None:
        """Put already-aligned lines in the log, without rewrapping them."""
        indent = " " * GUTTER
        self._emit(
            "\n".join(self.ink(colour, f"{indent}{row}") if row.strip() else "" for row in rows)
        )

    def banner(self, tagline: str, version:str, hint: str) -> None:
        """Open the session with the title, the weather, and how to get out."""
        rule = self.glyphs.rule * self._body_width
        title = f"{_spaced('CABIN')}   {_spaced('FEVER')}"
        self._emit(
            "\n".join(
                (
                    self.ink("note", rule),
                    f"{self.ink('title', title)}   {self.ink('accent', _spaced('x86'))}",
                    self.ink("note", rule),
                    self.ink("note", tagline),
                    "",
                    self.ink("note", version),
                    self.ink("note", hint),
                )
            )
        )

    def clear(self) -> None:
        """Wipe the screen, keeping the session going."""
        if self._out.isatty():
            self._write("\x1b[2J\x1b[H")

    def format(self, kind: str, text: str) -> list[str]:
        """Wrap one transmission into the speaker column, painted and ready."""
        label, label_colour, body_colour = KINDS[kind]
        head = f"{label} {self.glyphs.tick} " if label else " " * GUTTER
        width = self._body_width

        wrapped: list[str] = []
        for paragraph in text.strip("\n").split("\n"):
            wrapped.extend(textwrap.wrap(paragraph.strip(), width) if paragraph.strip() else [""])
        if not wrapped:
            wrapped = [""]

        painted = [self.ink(body_colour, line) if line else "" for line in wrapped]
        indent = " " * GUTTER
        first = self.ink(label_colour, head) if label else head
        rows = [f"{first}{painted[0]}", *(f"{indent}{line}" for line in painted[1:])]
        # A row holding nothing is a paragraph break, not GUTTER spaces of
        # trailing whitespace.
        return [row if row.strip() else "" for row in rows]

    def _emit(self, block: str) -> None:
        """Put a finished block of lines on the screen.

        Every block leaves a blank line behind it, so one exchange reads as
        one thing — and the prompt, which is drawn on that blank line, has
        room above it until the next block comes along and takes the row.
        """
        if not self._out.isatty():  # a file or a pipe; leave it clean
            self._write(f"{block}\n\n")
            return
        # There is a prompt on this row and no way to erase it, so it is
        # written over with spaces before the block goes anywhere near it.
        wipe = f"\r{' ' * len(self.prompt)}\r"
        self._write(f"{wipe}{block}\n\n{self.ink('note', self.prompt)}")

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()

    # -- what comes off the keyboard --

    async def lines(self) -> AsyncIterator[str]:
        """Yield what the player types, until they stop."""
        async for line in _stdin_lines():
            if self._out.isatty():
                self._write(self.ink("note", self.prompt))
            yield line.rstrip("\n")

    async def __aenter__(self) -> Console:
        """Take the terminal, if there is one to take."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Hand it back."""


class LiveConsole(Console):
    """A terminal held open: the log scrolls, the bottom row stays put.

    That row is the prompt. Everything that prints erases it first and draws
    it again after, so the line being typed is never the thing that gets
    scrolled away.
    """

    def __init__(
        self,
        out: TextIO,
        ink: Ink,
        glyphs: Glyphs,
        completions: Sequence[str] = (),
    ) -> None:
        """Draw on *out*; complete slash commands out of *completions*."""
        super().__init__(out, ink, glyphs)
        self._fd = -1  # the terminal is only taken hold of on the way in
        self._completions = sorted(completions)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

        self._buffer = ""
        self._cursor = 0
        self._scroll = 0
        self._pending = ""  # a keystroke that arrived in pieces
        self._history: list[str] = []
        self._index = 0
        self._draft = ""

        self._shown = False  # whether there is a prompt on the screen to erase
        self._armed = False  # a Ctrl-C waiting to see whether a second follows
        self._closed = False
        self._saved: list | None = None

    # -- the terminal --

    async def __aenter__(self) -> LiveConsole:
        """Put the terminal in cbreak mode and start reading it."""
        self._fd = sys.stdin.fileno()
        self._saved = termios.tcgetattr(self._fd)
        try:
            tty.setcbreak(self._fd)  # ISIG stays on, so Ctrl-C is still a signal

            loop = asyncio.get_running_loop()
            loop.add_reader(self._fd, self._readable)
            for sig, handler in ((signal.SIGWINCH, self._render), (signal.SIGINT, self._interrupt)):
                with contextlib.suppress(NotImplementedError, ValueError):
                    loop.add_signal_handler(sig, handler)
            self._render()
        except BaseException:
            # Half a terminal is worse than none. __aexit__ never runs for a
            # context that was never entered, so the settings go back here.
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Take the block back down and give the terminal its settings back."""
        self._closed = True
        loop = asyncio.get_running_loop()
        with contextlib.suppress(ValueError, OSError):
            loop.remove_reader(self._fd)
        for sig in (signal.SIGWINCH, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)

        self._write(f"{self._erase()}\x1b[?25h")
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)

    # -- the prompt at the bottom --

    def clear(self) -> None:
        """Wipe the screen and put the prompt back at the top of it."""
        self._shown = False  # whatever was down there went with the screen
        self._write("\x1b[2J\x1b[H")
        self._render()

    def _emit(self, block: str) -> None:
        # The block takes the row the prompt was sitting on, which is the
        # blank line the block before it left; the prompt moves down to this
        # one's. Nothing between them ever gets scrolled away.
        self._write(f"\x1b[?25l{self._erase()}{block}\n\n")
        self._render()

    def _erase(self) -> str:
        """Wipe the prompt, leaving the cursor at the start of its row."""
        if not self._shown:
            return ""
        self._shown = False
        return "\r\x1b[2K"

    def _render(self) -> None:
        """Draw the prompt, and leave the cursor in the middle of the sentence."""
        if self._closed:
            return
        visible, offset = self._window(max(self.columns, 20))

        parts = ["\x1b[?25l", self._erase(), f"{self.ink('note', self.prompt)}{visible}", "\r"]
        column = len(self.prompt) + offset
        if column:
            parts.append(f"\x1b[{column}C")
        parts.append("\x1b[?25h")

        self._shown = True
        self._write("".join(parts))

    def _window(self, columns: int) -> tuple[str, int]:
        """Take the slice of the line to show, and where the cursor sits in it.

        The prompt is one row however long the line gets: past the width of
        the terminal it scrolls sideways, which keeps the block a known height
        and the arithmetic above honest.
        """
        room = max(columns - len(self.prompt) - 1, 8)
        if len(self._buffer) <= room:
            self._scroll = 0
            return self._buffer, self._cursor

        start = min(self._scroll, self._cursor)
        start = max(start, self._cursor - room)
        start = max(0, min(start, len(self._buffer) - room))
        self._scroll = start

        visible = self._buffer[start : start + room]
        if start:
            visible = self.glyphs.more + visible[1:]
        if start + room < len(self._buffer):
            visible = visible[:-1] + self.glyphs.more
        return visible, self._cursor - start

    # -- the keyboard --

    async def lines(self) -> AsyncIterator[str]:
        """Yield finished lines, until the player hangs up."""
        while True:
            line = await self._queue.get()
            if line is None:
                return
            yield line

    def _readable(self) -> None:
        """Drain whatever the terminal has for us."""
        try:
            data = os.read(self._fd, 4096)
        except OSError:  # the terminal went away mid-read
            data = b""
        if not data:
            self._finish()
            return
        self._feed(self._decoder.decode(data))

    def _feed(self, keys: str) -> None:
        """Apply keystrokes, holding back any that arrived half-finished."""
        self._armed = False  # typing anything answers the "Ctrl-C again?"
        self._pending += keys
        while self._pending:
            taken = self._key(self._pending)
            if not taken:  # an escape sequence still on its way
                break
            self._pending = self._pending[taken:]
        self._render()

    def _key(self, keys: str) -> int:
        """Handle the first keystroke in *keys*, returning how much it took.

        Zero means the sequence is unfinished and the rest has not arrived.
        """
        char = keys[0]
        if char == "\x1b":
            return self._escape(keys)
        if char in "\r\n":
            self._submit()
        elif char in "\x7f\x08":  # backspace
            self._delete(-1)
        elif char == "\x04":  # Ctrl-D: forward delete, or hang up on an empty line
            if self._buffer:
                self._delete(1)
            else:
                self._finish()
        elif char == "\x01":  # Ctrl-A
            self._cursor = 0
        elif char == "\x05":  # Ctrl-E
            self._cursor = len(self._buffer)
        elif char == "\x02":  # Ctrl-B
            self._move(-1)
        elif char == "\x06":  # Ctrl-F
            self._move(1)
        elif char == "\x15":  # Ctrl-U
            self._buffer = self._buffer[self._cursor :]
            self._cursor = 0
        elif char == "\x0b":  # Ctrl-K
            self._buffer = self._buffer[: self._cursor]
        elif char == "\x17":  # Ctrl-W
            self._kill_word()
        elif char == "\x0c":  # Ctrl-L
            self._shown = False
            self._write("\x1b[2J\x1b[H")
        elif char == "\x10":  # Ctrl-P
            self._recall(-1)
        elif char == "\x0e":  # Ctrl-N
            self._recall(1)
        elif char == "\t":
            self._complete()
        elif char >= " ":
            # Take the whole run at once, so a paste lands as a paste.
            run = 0
            while run < len(keys) and keys[run] >= " " and keys[run] != "\x7f":
                run += 1
            self._insert(keys[:run])
            return run
        return 1

    def _escape(self, keys: str) -> int:
        """Handle one escape sequence, returning how many characters it took."""
        if len(keys) == 1:
            return 0  # a bare ESC so far; wait to see what follows it
        if keys[1] not in "[O":
            return 2  # Alt-<key>; nothing is bound to it
        for index in range(2, len(keys)):
            if "@" <= keys[index] <= "~":
                self._csi(keys[2:index], keys[index])
                return index + 1
        return 0

    def _csi(self, params: str, final: str) -> None:
        """Handle the arrow keys, and the ones near them."""
        if final == "A":
            self._recall(-1)
        elif final == "B":
            self._recall(1)
        elif final == "C":
            self._move(1)
        elif final == "D":
            self._move(-1)
        elif final == "H" or (final == "~" and params in {"1", "7"}):
            self._cursor = 0
        elif final == "F" or (final == "~" and params in {"4", "8"}):
            self._cursor = len(self._buffer)
        elif final == "~" and params == "3":
            self._delete(1)

    # -- editing --

    def _insert(self, text: str) -> None:
        self._buffer = f"{self._buffer[: self._cursor]}{text}{self._buffer[self._cursor :]}"
        self._cursor += len(text)

    def _move(self, step: int) -> None:
        self._cursor = min(max(self._cursor + step, 0), len(self._buffer))

    def _delete(self, step: int) -> None:
        if step < 0 and self._cursor:
            self._buffer = self._buffer[: self._cursor - 1] + self._buffer[self._cursor :]
            self._cursor -= 1
        elif step > 0 and self._cursor < len(self._buffer):
            self._buffer = self._buffer[: self._cursor] + self._buffer[self._cursor + 1 :]

    def _kill_word(self) -> None:
        index = self._cursor
        while index and self._buffer[index - 1].isspace():
            index -= 1
        while index and not self._buffer[index - 1].isspace():
            index -= 1
        self._buffer = self._buffer[:index] + self._buffer[self._cursor :]
        self._cursor = index

    def _recall(self, step: int) -> None:
        """Walk the history, keeping whatever was being typed when we left it."""
        if not self._history:
            return
        if self._index == len(self._history):
            self._draft = self._buffer
        self._index = min(max(self._index + step, 0), len(self._history))
        self._buffer = (
            self._draft if self._index == len(self._history) else self._history[self._index]
        )
        self._cursor = len(self._buffer)

    def _complete(self) -> None:
        """Finish a slash command, or show the ones that would fit."""
        if not self._buffer.startswith("/") or " " in self._buffer:
            return
        if self._cursor != len(self._buffer):
            return
        matches = [name for name in self._completions if name.startswith(self._buffer)]
        if not matches:
            return
        if len(matches) == 1:
            self._buffer = f"{matches[0]} "
        elif (shared := _common_prefix(matches)) != self._buffer:
            self._buffer = shared  # as far as they agree; press it again to see them
        else:
            self.listing(["  ".join(matches)])
        self._cursor = len(self._buffer)

    def _submit(self) -> None:
        """Hand the finished line over and start a new one."""
        line = self._buffer
        self._buffer = ""
        self._cursor = 0
        self._scroll = 0
        if line.strip() and (not self._history or self._history[-1] != line):
            self._history.append(line)
        self._index = len(self._history)
        self._draft = ""
        self._queue.put_nowait(line)

    # -- hanging up --

    def _interrupt(self) -> None:
        """Ctrl-C: drop the line being typed, or hang up if there isn't one."""
        if self._buffer:
            self._buffer = ""
            self._cursor = 0
            self._scroll = 0
            self._render()
            return
        if self._armed:
            self._finish()
            return
        self._armed = True
        self.say("note", "Ctrl-C again, or /quit, to hang up.")
        asyncio.get_running_loop().call_later(INTERRUPT_GRACE, self._disarm)

    def _disarm(self) -> None:
        self._armed = False

    def _finish(self) -> None:
        """Tell whoever is reading lines that there are no more of them."""
        self._queue.put_nowait(None)


def make_console(completions: Sequence[str] = (), color: bool = True) -> Console:
    """Build the best console this terminal can be driven as."""
    ink = Ink.detect(sys.stdout) if color else Ink(enabled=False)
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    glyphs = UNICODE if encoding.startswith("utf") else ASCII

    if RAW_MODE_AVAILABLE and sys.stdin.isatty() and sys.stdout.isatty():
        return LiveConsole(sys.stdout, ink, glyphs, completions)
    return Console(sys.stdout, ink, glyphs)


async def _stdin_lines() -> AsyncIterator[str]:
    """Yield lines from stdin without blocking the event loop.

    The read runs on a daemon thread so a disconnect can tear the client down
    while stdin is still parked waiting for the player to hit enter.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    def pump() -> None:
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(queue.put_nowait, line)
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except RuntimeError:  # the loop closed while we were blocked on stdin
            pass

    threading.Thread(target=pump, name="stdin-reader", daemon=True).start()

    while True:
        line = await queue.get()
        if line is None:  # EOF
            return
        yield line
