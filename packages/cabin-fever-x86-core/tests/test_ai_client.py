"""Which provider gets asked for what, when a client is built.

Nothing here reaches the network: every assertion is about the arguments the
client has been set up to send, not about a reply coming back.
"""

from __future__ import annotations

from typing import Any

import pytest

from cabin_fever_x86_core.config import AIClientConfig
from cabin_fever_x86_core.server._ai_client import PROMPT_CACHE_RETENTION, create_client

SESSION = "0eb1e5c6-2b62-4a4f-9a9d-3d3cf4b9a0f1"


def bound(client: Any) -> dict[str, Any]:
    """The arguments create_client has pinned onto every responses.create call."""
    return dict(getattr(client.responses.create, "keywords", {}))


def test_openai_asks_to_keep_a_cached_prefix_for_a_day() -> None:
    client, _model = create_client(
        AIClientConfig(provider="openai", api_key="not-a-real-key", model="gpt-5.4"), SESSION
    )

    assert bound(client)["prompt_cache_retention"] == "24h"


def test_the_gateway_is_left_to_its_own_caching() -> None:
    # Whatever sits behind a gateway may not understand the parameter, so it is
    # not sent; the session id it does expect still is.
    client, _model = create_client(
        AIClientConfig(
            provider="gateway",
            api_key="not-a-real-key",
            model="gpt-5.4",
            base_url="https://example.invalid/v1/",
        ),
        SESSION,
    )

    keywords = bound(client)
    assert "prompt_cache_retention" not in keywords
    assert keywords["extra_body"]["session_id"] == SESSION


def test_azure_is_never_asked_for_a_retention_it_refuses() -> None:
    # Azure rejects the parameter outright, which would fail every turn, every
    # compaction and every hint rather than merely costing more.
    assert PROMPT_CACHE_RETENTION["azure"] is None


@pytest.mark.parametrize("provider", ["openai", "azure", "gateway"])
def test_every_provider_says_what_it_wants(provider: str) -> None:
    # A provider added without an answer here would raise a KeyError on its
    # first session rather than quietly sending nothing.
    assert provider in PROMPT_CACHE_RETENTION
