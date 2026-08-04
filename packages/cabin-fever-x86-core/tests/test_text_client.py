"""The parts of the text client that can be checked without a terminal."""

import io
import os

import pytest

from cabin_fever_x86_core.text_client._console import (
    ASCII,
    GUTTER,
    UNICODE,
    Console,
    Ink,
    LiveConsole,
    _xterm256,
)
from cabin_fever_x86_core.text_client._main import (
    COMMANDS,
    LATEST,
    _parse_args,
    _resume_argument,
)

PLAIN = Ink(enabled=False)


def make_console(cls=Console):
    return cls(io.StringIO(), PLAIN, UNICODE)


# --- ink --------------------------------------------------------------------


def test_ink_off_leaves_text_alone():
    assert PLAIN("body", "hello") == "hello"


def test_ink_truecolor_and_256_wrap_the_same_text():
    assert Ink(truecolor=True)("accent", "x86") == "\x1b[38;2;122;192;122mx86\x1b[0m"
    assert Ink(truecolor=False)("accent", "x86") == "\x1b[38;5;114mx86\x1b[0m"


@pytest.mark.parametrize(
    ("rgb", "index"),
    [((0, 0, 0), 16), ((255, 255, 255), 231), ((128, 128, 128), 244), ((255, 0, 0), 196)],
)
def test_xterm256_lands_on_the_expected_cell(rgb, index):
    assert _xterm256(*rgb) == index


def test_ink_detect_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    stream.isatty = lambda: True
    assert not Ink.detect(stream).enabled


# --- the shape of a line ----------------------------------------------------


def test_a_line_hangs_off_the_speaker_column():
    console = make_console()
    lines = console.format("assistant", "word " * 60)

    assert lines[0].startswith(f"sam {UNICODE.tick} ")
    assert len(lines) > 1
    assert all(line.startswith(" " * GUTTER) for line in lines[1:])
    assert all(len(line) <= console.wrap_width for line in lines)


def test_paragraphs_survive_wrapping():
    lines = make_console().format("assistant", "first\n\nsecond")
    assert lines == [f"sam {UNICODE.tick} first", "", f"{' ' * GUTTER}second"]


def test_no_line_ends_in_whitespace():
    console = make_console()
    console.say("assistant", "first\n\nsecond")
    console.listing(["a heading", "", "  a row"])

    assert not any(line != line.rstrip() for line in console._out.getvalue().splitlines())


def test_a_note_has_no_speaker():
    assert make_console().format("note", "quiet") == [f"{' ' * GUTTER}quiet"]


def test_ascii_glyphs_keep_the_gutter_width():
    console = Console(io.StringIO(), PLAIN, ASCII)
    assert len(console.prompt) == GUTTER
    assert console.format("user", "hi") == [f"you {ASCII.tick} hi"]


def test_the_weather_goes_inside_the_title(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr("shutil.get_terminal_size", lambda _=None: os.terminal_size((40, 24)))
    Console(out, PLAIN, UNICODE).banner(
        "Nine days of rain.", "core version 0.0.0", "/help for commands"
    )

    # The banner is flush against the edge; only the log hangs off the gutter.
    rule = UNICODE.rule * (39 - GUTTER)  # the log's width, less the speaker column
    assert out.getvalue().splitlines() == [
        rule,
        "C A B I N   F E V E R   x 8 6",
        rule,
        "Nine days of rain.",
        "",
        "core version 0.0.0",
        "/help for commands",
        "",
    ]


def test_one_exchange_is_kept_off_the_last():
    console = make_console()
    console.say("assistant", "evening")
    console.say("user", "hello")

    assert console._out.getvalue() == (
        f"sam {UNICODE.tick} evening\n\nyou {UNICODE.tick} hello\n\n"
    )


def test_a_pipe_gets_the_line_and_nothing_else():
    out = io.StringIO()
    out.isatty = lambda: False
    Console(out, PLAIN, UNICODE).say("assistant", "evening")
    assert out.getvalue() == f"sam {UNICODE.tick} evening\n\n"


def test_a_terminal_without_raw_mode_gets_its_prompt_written_over():
    out = io.StringIO()
    out.isatty = lambda: True
    console = Console(out, PLAIN, UNICODE)
    console.say("assistant", "evening")
    console.say("user", "hello")

    wipe = f"\r{' ' * GUTTER}\r"
    assert out.getvalue() == (
        f"{wipe}sam {UNICODE.tick} evening\n\nyou {UNICODE.tick} "
        f"{wipe}you {UNICODE.tick} hello\n\nyou {UNICODE.tick} "
    )


# --- the line editor --------------------------------------------------------


def editor(**kwargs):
    """A live console with a terminal it will never actually touch."""
    console = LiveConsole(io.StringIO(), PLAIN, UNICODE, **kwargs)
    console._closed = True  # nothing to render onto
    return console


def typed(console):
    return console._buffer, console._cursor


def test_typing_and_backspace():
    console = editor()
    console._feed("open the mailboxx")
    console._feed("\x7f")
    assert typed(console) == ("open the mailbox", 16)


def test_arrows_move_and_insert_in_the_middle():
    console = editor()
    console._feed("open mailbox")
    console._feed("\x1b[D" * 7)  # back over "mailbox"
    console._feed("the ")
    assert typed(console) == ("open the mailbox", 9)


def test_home_end_and_the_kill_keys():
    console = editor()
    console._feed("take the lamp")
    console._feed("\x01")  # Ctrl-A
    assert console._cursor == 0
    console._feed("\x05")  # Ctrl-E
    assert console._cursor == 13
    console._feed("\x17")  # Ctrl-W eats one word
    assert typed(console) == ("take the ", 9)
    console._feed("\x15")  # Ctrl-U eats the rest
    assert typed(console) == ("", 0)


def test_forward_delete():
    console = editor()
    console._feed("north")
    console._feed("\x01\x1b[3~")
    assert typed(console) == ("orth", 0)


def test_an_escape_sequence_split_across_reads_is_held():
    console = editor()
    console._feed("go")
    console._feed("\x1b")
    assert console._pending == "\x1b"
    console._feed("[")
    assert console._pending == "\x1b["
    console._feed("D")
    assert (console._pending, console._cursor) == ("", 1)


def test_enter_queues_the_line_and_keeps_the_history():
    console = editor()
    console._feed("open mailbox\r")
    assert console._queue.get_nowait() == "open mailbox"
    assert typed(console) == ("", 0)

    console._feed("\x1b[A")  # up
    assert typed(console) == ("open mailbox", 12)
    console._feed("\x1b[B")  # and back down to the empty line we left
    assert typed(console) == ("", 0)


def test_history_keeps_an_unfinished_line():
    console = editor()
    console._feed("read the leaflet\r")
    console._queue.get_nowait()
    console._feed("half a thou")
    console._feed("\x1b[A")
    assert console._buffer == "read the leaflet"
    console._feed("\x1b[B")
    assert console._buffer == "half a thou"


def test_blank_lines_stay_out_of_the_history():
    console = editor()
    console._feed("   \r")
    assert console._queue.get_nowait() == "   "
    assert console._history == []


def test_ctrl_d_on_an_empty_line_hangs_up():
    console = editor()
    console._feed("\x04")
    assert console._queue.get_nowait() is None


def test_tab_finishes_a_command():
    console = editor(completions=COMMANDS)
    console._feed("/cl\t")
    assert console._buffer == "/clear "


def test_tab_stops_where_the_commands_stop_agreeing():
    console = editor(completions=COMMANDS)
    console._feed("/se\t")
    assert console._buffer == "/session"  # /session and /sessions, so far


def test_tab_finishes_a_command_an_alias_is_a_prefix_of():
    # "/q" is an alias for /quit, but it is kept out of the completions so it
    # cannot stand in the way of the command it is short for.
    console = editor(completions=COMMANDS)
    console._feed("/q\t")
    assert console._buffer == "/quit "


def test_tab_on_nothing_recognisable_does_nothing():
    console = editor(completions=COMMANDS)
    console._feed("/zork\t")
    assert console._buffer == "/zork"


def test_a_paste_arrives_in_one_piece():
    console = editor()
    console._feed("open the small mailbox and read the leaflet inside it")
    assert console._cursor == len(console._buffer)


# --- the prompt window ------------------------------------------------------


def test_a_short_line_is_shown_whole():
    console = editor()
    console._feed("look")
    assert console._window(80) == ("look", 4)


def test_a_long_line_scrolls_sideways_under_the_cursor():
    console = editor()
    console._feed("x" * 200)
    visible, offset = console._window(40)

    assert len(visible) == 40 - GUTTER - 1
    assert visible.startswith(UNICODE.more)  # the start has scrolled away
    assert offset == len(visible)


def test_scrolling_back_to_the_start_drops_the_marker():
    console = editor()
    console._feed("x" * 200)
    console._window(40)
    console._feed("\x01")  # Ctrl-A, back to the beginning
    visible, offset = console._window(40)

    assert not visible.startswith(UNICODE.more)
    assert visible.endswith(UNICODE.more)  # ... but the end has
    assert offset == 0


# --- arguments --------------------------------------------------------------


def test_resume_argument_reads_ids_and_latest():
    assert _resume_argument("latest") == LATEST
    assert str(_resume_argument("6b1f6d0e-6f9e-4a35-9f2e-2c1a9b5d4e33")).startswith("6b1f6d0e")


def test_resume_argument_rejects_nonsense():
    with pytest.raises(Exception, match="not a session id"):
        _resume_argument("the one from tuesday")


def test_continue_selects_the_latest_session():
    args = _parse_args(["--continue"])

    assert args.continue_latest
    assert args.resume is None


def test_continue_and_resume_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _parse_args(["--continue", "--resume"])
