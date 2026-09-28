#!/usr/bin/env python3
"""Generate fresh Sam greetings, one per stdout line, using CF86_OPENAI_API_KEY.

Run from this repository with its core package installed:

    uv run --package cabin-fever-x86-core generate_openings.py --count 5 >> openings.txt

Each sample uses the game's full system prompt, opening stage direction, tool
definitions, and model settings, but forces ``transmit`` like the game's final
model round. A stable prompt cache key is shared across samples and runs;
no conversation is carried between samples. This never starts a game session,
reads the greeting cache, executes tools, or generates audio.

Each response's token usage and estimated cost at standard GPT-5.4 rates are
printed to stderr with a running dollar total for this run. Missing usage or
unpriced models/tiers are reported there and excluded from the known total.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

from openai import OpenAIError
from openai.types.responses import Response

from cabin_fever_x86_core.config import AIClientConfig, ServerConfig
from cabin_fever_x86_core.messages import AssistantMessage
from cabin_fever_x86_core.server._ai_client import create_client
from cabin_fever_x86_core.server._game import OPENING_DIRECTION, STAGE_DIRECTION, Game, Interruption
from cabin_fever_x86_core.server._system_prompt import SYSTEM_PROMPT
from cabin_fever_x86_core.server._tools import RequestHintTool, TransmitTool

# Cache routing is separate from conversation state. Keep this stable so the
# identical opening context can be reused across samples and script runs.
PROMPT_CACHE_KEY = "cf86-openings-v1"

# USD per million tokens, verified 2026-09-28:
# https://developers.openai.com/api/docs/models/gpt-5.4
GPT54_INPUT_RATE = 2.50
GPT54_CACHED_RATE = 0.25
GPT54_OUTPUT_RATE = 15.00


async def _unused_send(_message: AssistantMessage) -> None:
    """Guard against accidentally executing the game's transmission callback."""
    raise RuntimeError("The opening generator must capture replies without executing tools.")


def transmission(response: Response) -> str:
    """Extract one complete greeting from a transmit call or a plain reply."""
    if response.status != "completed":
        raise ValueError(f"Response did not complete (status: {response.status}).")

    calls = [item for item in response.output if item.type == "function_call"]
    if calls:
        if len(calls) != 1 or calls[0].name != TransmitTool.name:
            raise ValueError("Expected one transmit call.")
        arguments = json.loads(calls[0].arguments)
        message = arguments.get("message") if isinstance(arguments, dict) else None
    else:
        message = response.output_text

    if not isinstance(message, str) or not message.strip():
        raise ValueError("Response contained no greeting text.")

    # The cache format is one greeting per physical line, without JSON quoting.
    return " ".join(line.strip() for line in message.splitlines() if line.strip())


def cost_summary(response: Response) -> tuple[float | None, str]:
    """Return the unrounded dollar cost and its printable token breakdown."""
    usage = response.usage
    if usage is None:
        return None, "cost unavailable (API returned no token usage)"

    cached = usage.input_tokens_details.cached_tokens
    uncached = usage.input_tokens - cached
    tokens = f"input: {uncached} uncached + {cached} cached; output: {usage.output_tokens} tokens"
    if not re.fullmatch(r"gpt-5\.4(?:-\d{4}-\d{2}-\d{2})?", response.model):
        return None, f"cost unavailable (unpriced model: {response.model}; {tokens})"
    if response.service_tier not in (None, "default", "auto"):
        return None, f"cost unavailable (unpriced service tier: {response.service_tier}; {tokens})"

    # GPT-5.4's long-context premium starts above 272K input tokens. Ordinary
    # openings are much smaller, but retain the correct rate if the prompt grows.
    input_multiplier = 2 if usage.input_tokens > 272_000 else 1
    output_multiplier = 1.5 if usage.input_tokens > 272_000 else 1
    # output_tokens already includes reasoning_tokens; do not add those again.
    cost = (
        (uncached * GPT54_INPUT_RATE + cached * GPT54_CACHED_RATE) * input_multiplier
        + usage.output_tokens * GPT54_OUTPUT_RATE * output_multiplier
    ) / 1_000_000
    return cost, f"${cost:.6f} USD ({tokens})"


async def generate_opening(config: AIClientConfig) -> Response:
    """Make one independent opening request with the game's current context."""
    # Construction supplies the real tool definitions in the real order. Do not
    # enter Game's context manager: that would start workers and write a session.
    game = Game(ServerConfig(ai_client=config), _unused_send)
    session_id = str(game.session_id)
    client, model = create_client(config, session_id)
    async with client:
        # Game normally adds this definition on context entry, even when hints
        # are unavailable. Keep it in the prompt, just as in an actual opening.
        hint = RequestHintTool(client, model, game._machine)
        tools = [tool.definition for tool in game._tools.values()] + [hint.definition]
        opening = Interruption(kind=STAGE_DIRECTION, text=OPENING_DIRECTION)
        return await client.responses.create(
            model=model,
            prompt_cache_key=PROMPT_CACHE_KEY,
            instructions=SYSTEM_PROMPT,
            input=[{"role": "user", "content": opening.content}],
            tools=tools,
            tool_choice={"type": "function", "name": TransmitTool.name},
            parallel_tool_calls=False,
            reasoning={"effort": "medium"},
            store=False,
            include=["reasoning.encrypted_content"],
        )


async def generate(config: AIClientConfig, count: int) -> int:
    """Print greetings as they arrive; keep failures out of redirected stdout."""
    total = 0.0
    unpriced = 0
    for index in range(1, count + 1):
        try:
            response = await generate_opening(config)
            cost, summary = cost_summary(response)
            if cost is None:
                unpriced += 1
            else:
                total += cost
            total_summary = (
                f"known total=${total:.6f} USD ({unpriced} unpriced)"
                if unpriced
                else f"total=${total:.6f} USD"
            )
            # Even an incomplete or malformed reply may have incurred a charge.
            print(
                f"opening {index}/{count}: {summary}; {total_summary}",
                file=sys.stderr,
                flush=True,
            )
            text = transmission(response)
        except (OpenAIError, ValueError) as exc:
            print(f"error: opening {index}/{count}: {exc}", file=sys.stderr)
            return 1
        print(text, flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse the CLI and generate independent openings using the supplied key."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--count", "-n", type=int, default=5, help="Greetings to generate (default: 5)."
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model to use (default: OPENAI_MODEL, or gpt-5.4).",
    )
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be at least 1")
    api_key = os.environ.get("CF86_OPENAI_API_KEY", "").strip()
    if not api_key:
        parser.error("Set CF86_OPENAI_API_KEY before generating openings.")

    config = AIClientConfig(provider="openai", api_key=api_key, model=args.model)
    try:
        return asyncio.run(generate(config, args.count))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
