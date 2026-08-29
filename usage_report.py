#!/usr/bin/env python3
"""What a session's requests to the model cost, read back off its ledger.

Reads ``usage.jsonl`` — the append-only record the server writes beside each
session's conversation — and prices it.

    uv run usage_report.py <session-id> --raw-turn-costs
    uv run usage_report.py data/sessions/<session-id>/server --raw-turn-costs
    uv run usage_report.py path/to/usage.jsonl --raw-turn-costs

``--raw-turn-costs`` lists every request in the order it was made, grouped by
the turn it was serving. A hint or a compaction appears inside the turn that
caused it, because that is where the money actually went.

The ledger deliberately stores no prices — they change, and change retroactively
relative to a log — so they live here instead, in ``RATES``, and can be replaced
wholesale with ``--rates``. A model with no entry is an error naming it, rather
than a row quietly priced at nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from cabin_fever_x86_core.sessions import SERVER_COMPONENT, USAGE_FILE, session_dir

# Dollars per million tokens, by model, from the provider's price list. A model
# is matched exactly first, then by the longest entry it extends at a dash, so
# one line covers every dated build: gpt-5.4-2026-03-05 is priced as gpt-5.4,
# and never as gpt-5.
#
# Where the price list says "-" for cached input or for cache writes, the entry
# simply leaves that key out. It does not mean those tokens are free; it means
# the model has no such thing to charge for, so the report leaves the column out
# too rather than printing a row of zeroes.
#
# A model that reports tokens it names no price for is therefore a surprise, and
# is warned about: they are billed as the ordinary input they came in as, which
# keeps the total honest, and the warning says how many went that way.
#
# The gpt-5.4 and later families are listed at their *short context* prices. A
# request long enough to fall into a long-context tier will be under-counted.
RATES: dict[str, dict[str, float]] = {
    # model: uncached, cache_read, [cache_write], output
    "gpt-5.6-sol": {"uncached": 4.00, "cache_read": 0.40, "cache_write": 5.00, "output": 20.00},
    "gpt-5.6-terra": {"uncached": 2.00, "cache_read": 0.20, "cache_write": 2.50, "output": 12.00},
    "gpt-5.6-luna": {"uncached": 0.20, "cache_read": 0.02, "cache_write": 0.25, "output": 1.20},
    "gpt-5.5": {"uncached": 5.00, "cache_read": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"uncached": 30.00, "output": 180.00},
    "gpt-5.4": {"uncached": 2.50, "cache_read": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"uncached": 0.75, "cache_read": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"uncached": 0.20, "cache_read": 0.02, "output": 1.25},
    "gpt-5.4-pro": {"uncached": 30.00, "output": 180.00},
    "gpt-5.2": {"uncached": 1.75, "cache_read": 0.175, "output": 14.00},
    "gpt-5.2-pro": {"uncached": 21.00, "output": 168.00},
    "gpt-5.1": {"uncached": 1.25, "cache_read": 0.125, "output": 10.00},
    "gpt-5": {"uncached": 1.25, "cache_read": 0.125, "output": 10.00},
    "gpt-5-mini": {"uncached": 0.25, "cache_read": 0.025, "output": 2.00},
    "gpt-5-nano": {"uncached": 0.05, "cache_read": 0.005, "output": 0.40},
    "gpt-5-pro": {"uncached": 15.00, "output": 120.00},
    "gpt-4.1": {"uncached": 2.00, "cache_read": 0.50, "output": 8.00},
    "gpt-4.1-mini": {"uncached": 0.40, "cache_read": 0.10, "output": 1.60},
    "gpt-4.1-nano": {"uncached": 0.10, "cache_read": 0.025, "output": 0.40},
    "gpt-4o": {"uncached": 2.50, "cache_read": 1.25, "output": 10.00},
    "gpt-4o-mini": {"uncached": 0.15, "cache_read": 0.075, "output": 0.60},
    "gpt-4o-2024-05-13": {
        "uncached": 5.00,
        "output": 15.00,
    },
    "o4-mini": {"uncached": 1.10, "cache_read": 0.275, "output": 4.40},
    "o3": {"uncached": 2.00, "cache_read": 0.50, "output": 8.00},
    "o3-mini": {"uncached": 1.10, "cache_read": 0.55, "output": 4.40},
    "o3-pro": {"uncached": 20.00, "output": 80.00},
    "o1": {"uncached": 15.00, "cache_read": 7.50, "output": 60.00},
    "o1-pro": {"uncached": 150.00, "output": 600.00},
    "gpt-4-turbo-2024-04-09": {
        "uncached": 10.00,
        "output": 30.00,
    },
    "gpt-4-0613": {"uncached": 30.00, "output": 60.00},
    "gpt-3.5-turbo": {"uncached": 0.50, "output": 1.50},
    "gpt-3.5-turbo-0125": {
        "uncached": 0.50,
        "output": 1.50,
    },
    "gpt-3.5-turbo-1106": {
        "uncached": 1.00,
        "output": 2.00,
    },
    "gpt-3.5-turbo-instruct": {
        "uncached": 1.50,
        "output": 2.00,
    },
    "davinci-002": {"uncached": 2.00, "output": 2.00},
    "babbage-002": {"uncached": 0.40, "output": 0.40},
}

#: The four things a request is billed for. Input tokens arrive as one count
#: with the cached and newly-written parts broken out of it, so what is left
#: after both is what was charged at the full input rate.
RATE_FIELDS = ("uncached", "cache_read", "cache_write", "output")

PER = 1_000_000


@dataclass(frozen=True)
class Tokens:
    """One request's tokens, split the way they are priced."""

    uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0


@dataclass(frozen=True)
class Costs:
    """What one request came to, in dollars, split the same four ways.

    Totalled in dollars rather than in tokens, so a file that spans more than
    one model still adds up: the same count costs different amounts depending on
    what served it.
    """

    uncached: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    output: float = 0.0
    #: Whether the model charges for cached reads and cache writes at all. Where
    #: it does not, there is no such thing to show and the amount is not listed.
    priced_reads: bool = False
    priced_writes: bool = False
    #: The same amount said in output tokens: what it cost, over what one output
    #: token costs on the model that served it. It drops the model's price scale
    #: while keeping its shape, so the same work on a mini and on a flagship
    #: reads the same, and summing it across models stays meaningful because
    #: each request is converted at its own rate before being added.
    output_equivalent: float = 0.0

    @property
    def total(self) -> float:
        """What the four parts come to together."""
        return sum(getattr(self, field) for field in RATE_FIELDS)

    def __add__(self, other: Costs) -> Costs:
        """Add two splits together, part by part."""
        return Costs(
            uncached=self.uncached + other.uncached,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            output=self.output + other.output,
            priced_reads=self.priced_reads or other.priced_reads,
            priced_writes=self.priced_writes or other.priced_writes,
            output_equivalent=self.output_equivalent + other.output_equivalent,
        )

    def __str__(self) -> str:
        """Render the amounts the way the report lists them."""
        parts = []
        if self.priced_reads:
            parts.append(f"Cache read: ${self.cache_read:.4f}")
        if self.priced_writes:
            parts.append(f"Cache write: ${self.cache_write:.4f}")
        parts.append(f"Uncached: ${self.uncached:.4f}")
        parts.append(f"Output: ${self.output:.4f}")
        return ", ".join(parts)


def tokens_of(row: dict[str, Any]) -> Tokens:
    """Split one row's usage into what each part of it is billed at."""
    usage = row.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_tokens = usage.get("output_tokens", 0)
    cache_read = input_details.get("cached_tokens", 0)
    cache_write = input_details.get("cache_write_tokens", 0)
    # Both are reported as parts of input_tokens rather than beside it, so the
    # remainder is what was neither read from the cache nor written to it.
    uncached = max(0, usage.get("input_tokens", 0) - cache_read - cache_write)
    return Tokens(
        uncached=uncached,
        cache_read=cache_read,
        cache_write=cache_write,
        output=output_tokens,
    )


def rate_for(model: str | None, rates: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """Return the rate for *model*, matched exactly or as a dated build of one.

    The match has to fall on a dash, so gpt-5.4-2026-03-05 is priced as gpt-5.4
    and a model nothing covers goes unpriced rather than being quietly charged
    at the rate of whatever it happens to begin with.
    """
    if model is None:
        return None
    if model in rates:
        return rates[model]
    matches = [key for key in rates if model.startswith(f"{key}-")]
    if not matches:
        return None
    return rates[max(matches, key=len)]


def unpriced(tokens: Tokens, rate: dict[str, float]) -> list[str]:
    """Name the parts that carried tokens the model has no price for.

    Nothing is said about a part that was priced, or about one that was empty:
    a model with no cached-input rate is expected to report no cached tokens,
    and only the combination is a surprise.
    """
    return [
        field
        for field in ("cache_read", "cache_write")
        if getattr(tokens, field) and field not in rate
    ]


def overcounted(row: dict[str, Any]) -> tuple[int, int, int] | None:
    """Return the cached and written tokens of one row, if they exceed its input.

    Both are reported out of ``input_tokens``, so together they cannot come to
    more than it. If they ever do, the split every figure here rests on does not
    hold for that row.
    """
    usage = row.get("usage") or {}
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0)
    written = details.get("cache_write_tokens", 0)
    whole = usage.get("input_tokens", 0)
    return (cached, written, whole) if cached + written > whole else None


def priced(costs: Costs) -> str:
    """Render one amount and what it is in output tokens, in fixed columns.

    Both are right-aligned so that everything after them stays in line down the
    block; a figure wide enough to overflow its field pushes only its own row
    out rather than breaking the format.
    """
    amount = f"${costs.total:.4f}"
    return f"Cost = {amount:>8} ({round(costs.output_equivalent):>7,} output-eq)"


def costs_of(tokens: Tokens, rate: dict[str, float]) -> Costs:
    """Return what those tokens come to, in dollars, part by part.

    Tokens in a part the model names no price for are billed as the ordinary
    input they arrived as, so they weigh on the total rather than disappearing
    from it. That they arrived at all is worth a warning, which is :func:`unpriced`.
    """
    priced_reads = "cache_read" in rate
    priced_writes = "cache_write" in rate
    uncached = tokens.uncached
    if not priced_reads:
        uncached += tokens.cache_read
    if not priced_writes:
        uncached += tokens.cache_write
    costs = Costs(
        uncached=uncached * rate.get("uncached", 0.0) / PER,
        cache_read=tokens.cache_read * rate.get("cache_read", 0.0) / PER,
        cache_write=tokens.cache_write * rate.get("cache_write", 0.0) / PER,
        output=tokens.output * rate.get("output", 0.0) / PER,
        priced_reads=priced_reads,
        priced_writes=priced_writes,
    )
    # A rate table with no output price cannot say this in output tokens; the
    # dollars still stand, so the figure is left at nothing rather than refused.
    per_output = rate.get("output", 0.0)
    if not per_output:
        return costs
    return replace(costs, output_equivalent=costs.total / per_output * PER)


def load_rates(path: Path) -> dict[str, dict[str, float]]:
    """Read a rate table: ``{model: {uncached, cache_read, cache_write, output}}``."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: could not read rates from {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise SystemExit(f"error: {path} should hold an object keyed by model name")
    return loaded


def resolve(target: str) -> Path:
    """Find the ledger behind a session id, a session directory, or a file."""
    path = Path(target)
    if path.is_file():
        return path
    if path.is_dir():
        for candidate in (path / SERVER_COMPONENT / USAGE_FILE, path / USAGE_FILE):
            if candidate.is_file():
                return candidate
        raise SystemExit(f"error: no {USAGE_FILE} under {path}")
    try:
        session_id = UUID(target)
    except ValueError:
        raise SystemExit(f"error: {target} is not a file, a directory, or a session id") from None
    candidate = session_dir(session_id, SERVER_COMPONENT, create=False) / USAGE_FILE
    if not candidate.is_file():
        raise SystemExit(f"error: no ledger for session {session_id} at {candidate}")
    return candidate


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Every record in the ledger, in the order it was spent."""
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"warning: dropping {path.name} line {number}: {exc}", file=sys.stderr)
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            print(f"warning: dropping {path.name} line {number}: not an object", file=sys.stderr)
    return rows


def grouped(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Rows gathered under the turn they were spent on, in the order they arrived.

    A hint or an automatic compaction names the turn that caused it, so it is
    charged there rather than standing on its own. Anything whose parent is not
    in this file — a compaction the operator asked for, a truncated ledger —
    heads its own group.
    """
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    known = {row.get("turn_id") for row in rows}
    for row in rows:
        parent = row.get("parent_turn_id")
        key = parent if parent in known else row.get("turn_id")
        if key not in members:
            order.append(key)
        members[key].append(row)
    return [(key, members[key]) for key in order]


def label_for(row: dict[str, Any], key: str, siblings: list[dict[str, Any]]) -> str:
    """Name one request within its turn: a round of it, or work it caused."""
    if row.get("turn_id") == key:
        return f"round {row.get('model_round', 0) + 1}"
    turn_type = row.get("turn_type", "?")
    # Hints and compactions are one request each today; if that ever stops being
    # true, say which round of the nested turn this was.
    same_turn = [item for item in siblings if item.get("turn_id") == row.get("turn_id")]
    if len(same_turn) > 1:
        return f"{turn_type} round {row.get('model_round', 0) + 1}"
    return turn_type


def moment(row: dict[str, Any]) -> datetime | None:
    """When the ledger wrote this row down, if it says so readably."""
    stamp = row.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def fractional_digits(stamp: str) -> int:
    """How much of a second the timestamp actually carries.

    A gap is shown to the precision it was measured at and no further: the
    ledger writes milliseconds, so trailing zeroes would only claim an accuracy
    that was never recorded.
    """
    clock = stamp.rpartition("T")[2]
    # The zone offset also holds a colon and digits; drop it before looking for
    # the fraction of a second.
    for sign in ("+", "-"):
        head, found, _tail = clock.partition(sign)
        if found:
            clock = head
    _whole, dot, fraction = clock.rstrip("Zz").rpartition(".")
    return len(fraction) if dot else 0


def report_raw_turn_costs(rows: list[dict[str, Any]], rates: dict[str, dict[str, float]]) -> None:
    """Print every request, grouped by the turn it was serving."""
    missing = sorted(
        {
            model
            for row in rows
            if (model := row.get("model")) is not None and rate_for(model, rates) is None
        }
    )
    if missing:
        template = {model: dict.fromkeys(RATE_FIELDS, 0.0) for model in missing}
        raise SystemExit(
            "error: no rate for "
            + ", ".join(repr(model) for model in missing)
            + ".\nAdd them to RATES in this script, or pass --rates with a file like:\n"
            + json.dumps(template, indent=2)
            + "\n(dollars per million tokens)"
        )

    running = Costs()
    # Tokens that turned up in a part their model names no price for, gathered
    # so the surprise is reported once per model rather than once per request.
    surprises: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    # Rows whose parts came to more than the whole they were taken out of.
    impossible: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    # Where the last turn left off, so the gap to this one can be shown: a
    # prompt cache goes cold on its own, and a long quiet stretch is often the
    # whole explanation for the miss on the round that follows it. Measured from
    # the previous turn's *last* request rather than its first, because what
    # matters to the cache is when it was last touched, not when the turn that
    # touched it began.
    previous: datetime | None = None
    previous_digits = 0
    for key, members in grouped(rows):
        head = next((row for row in members if row.get("turn_id") == key), members[0])
        print(f"Turn id: {key}")
        print(f"Turn type: {head.get('turn_type', '?')}")

        # The first request of the turn, as the ledger recorded it rather than
        # reformatted, with the quiet since the last request before it.
        started = members[0].get("timestamp")
        if isinstance(started, str):
            when = moment(members[0])
            gap = ""
            if when is not None and previous is not None:
                seconds = (when - previous).total_seconds()
                # No finer than the coarser of the two stamps it came from.
                digits = min(fractional_digits(started), previous_digits)
                gap = f" ({seconds:+.{digits}f} seconds)"
            print(f"Time: {started}{gap}")

        # Carried from the end of this turn, not its beginning.
        ended = members[-1].get("timestamp")
        finished = moment(members[-1])
        if finished is not None and isinstance(ended, str):
            previous, previous_digits = finished, fractional_digits(ended)

        totals = Costs()
        for row in members:
            rate = rate_for(row.get("model"), rates) or {}
            tokens = tokens_of(row)
            costs = costs_of(tokens, rate)
            totals = totals + costs
            for field in unpriced(tokens, rate):
                seen = surprises[(str(row.get("model")), field)]
                seen[0] += 1
                seen[1] += getattr(tokens, field)
            broken = overcounted(row)
            if broken is not None:
                tally = impossible[str(row.get("model"))]
                tally[0] += 1
                for at, count in enumerate(broken, start=1):
                    tally[at] += count
            # Not a word about models that have no cache to miss: on those,
            # every request reads nothing back and the note would be noise.
            miss = " [CACHE MISS]" if costs.priced_reads and not tokens.cache_read else ""
            print(f"    {label_for(row, key, members)}: {priced(costs)}  {costs}{miss}")

        print(f"Total: {priced(totals)}  {totals}")
        print()
        running = running + totals

    print(f"All turns: {priced(running)}  {running}")

    # The report is on stdout and the warnings on stderr, so that a redirected
    # report stays clean. Flushed first, or the two arrive out of order when
    # stdout is a file and stderr is a terminal.
    sys.stdout.flush()
    for (model, field), (requests, tokens) in sorted(surprises.items()):
        print(
            f"warning: {requests} request(s) on {model!r} reported {tokens:,} {field} tokens, "
            f"which it names no price for; billed as ordinary input.",
            file=sys.stderr,
        )
    for model, (requests, cached, written, whole) in sorted(impossible.items()):
        print(
            f"warning: {requests} request(s) on {model!r} reported {cached:,} cached + "
            f"{written:,} written tokens against {whole:,} input; "
            f"the parts exceed the whole",
            file=sys.stderr,
        )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "target",
        help=f"A session id, a session directory, or a {USAGE_FILE} to read.",
    )
    parser.add_argument(
        "--raw-turn-costs",
        action="store_true",
        help="List every request, grouped by the turn it was serving.",
    )
    parser.add_argument(
        "--rates",
        type=Path,
        help="A JSON rate table, in dollars per million tokens, keyed by model.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Read the ledger the arguments point at, and print the report asked for."""
    args = _parse_args()
    if not args.raw_turn_costs:
        raise SystemExit("error: nothing to do; pass --raw-turn-costs")

    rates = load_rates(args.rates) if args.rates is not None else dict(RATES)

    path = resolve(args.target)
    rows = load_rows(path)
    if not rows:
        raise SystemExit(f"error: {path} holds no records")

    report_raw_turn_costs(rows, rates)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        # Piped into head or a pager that closed early. Point what is left of
        # stdout at nothing, so the interpreter's own flush on the way out does
        # not raise this a second time.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
