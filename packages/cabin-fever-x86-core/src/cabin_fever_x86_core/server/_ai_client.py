"""Factory for creating an AsyncOpenAI client from an AIClientConfig."""

import os
from functools import partial
from typing import assert_never

from openai import AsyncOpenAI

from cabin_fever_x86_core.config import AIClientConfig, Provider

# The standard Azure AI scope. Any other endpoint — a research gateway, a
# private deployment — sets its own through config or AZURE_SCOPE.
AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"

# How long each provider may be asked to keep a cached prompt prefix alive.
#
# Every request resends the whole conversation, so a prefix that is still cached
# is most of what a turn costs, and the default for an organisation with zero
# data retention is the short-lived one. Asking for longer is worth real money.
#
# But the parameter is not universal: Azure refuses it outright, and what a
# gateway in front of somebody else's deployment will accept is its own
# business. It is therefore sent only where it is known to be understood, and a
# provider mapped to None is simply never asked.
PROMPT_CACHE_RETENTION: dict[Provider, str | None] = {
    "openai": "24h",
    "azure": None,
    "gateway": None,
}


def create_client(config: AIClientConfig, session_id: str) -> tuple[AsyncOpenAI, str]:
    """Create an AsyncOpenAI client and return (client, model) based on config.provider.

    For each setting the resolution order is: config field → env var → built-in default.
    """
    if config.provider == "openai":
        client, model = _create_openai(config)
    elif config.provider == "azure":
        client, model = _create_azure(config)
    elif config.provider == "gateway":
        client, model = _create_gateway(config, session_id=session_id)
    else:
        assert_never(config.provider)

    # Bound here rather than at the call sites: how long a prefix may be cached
    # is a property of the provider, and the code asking for a reply has no
    # business knowing which ones support it.
    retention = PROMPT_CACHE_RETENTION[config.provider]
    if retention is not None:
        client.responses.create = partial(  # ty:ignore[invalid-assignment]
            client.responses.create,
            prompt_cache_retention=retention,
        )

    return client, model


def _create_openai(config: AIClientConfig) -> tuple[AsyncOpenAI, str]:
    model = config.model or os.environ.get("OPENAI_MODEL", "gpt-5.4")
    base_url = config.base_url or os.environ.get("OPENAI_BASE_URL") or None
    api_key = config.api_key or os.environ.get("OPENAI_API_KEY") or None

    kwargs: dict = {}
    if base_url is not None:
        kwargs["base_url"] = base_url
    if api_key is not None:
        kwargs["api_key"] = api_key

    return AsyncOpenAI(max_retries=5, **kwargs), model


def _create_azure(config: AIClientConfig) -> tuple[AsyncOpenAI, str]:
    from azure.identity import (
        AzureCliCredential,
        ChainedTokenCredential,
        ManagedIdentityCredential,
        get_bearer_token_provider,
    )

    model = config.model or os.environ.get("AZURE_MODEL")
    if not model:
        raise ValueError(
            "The azure provider needs a model (server.ai_client.model or the AZURE_MODEL env var)."
        )

    scope = config.scope or os.environ.get("AZURE_SCOPE") or AZURE_SCOPE

    base_url = config.base_url
    if not base_url:
        endpoint = os.environ.get("AZURE_ENDPOINT")
        if not endpoint:
            raise ValueError(
                "The azure provider needs an endpoint "
                "(server.ai_client.base_url, or AZURE_ENDPOINT to have "
                "/openai/v1/ appended to it)."
            )
        base_url = f"{endpoint.rstrip('/')}/openai/v1/"

    credential = get_bearer_token_provider(
        ChainedTokenCredential(AzureCliCredential(), ManagedIdentityCredential()),
        scope,
    )

    async def async_credential() -> str:
        return credential()

    client = AsyncOpenAI(max_retries=5, base_url=base_url, api_key=async_credential)  # type: ignore[arg-type]
    return client, model


def _create_gateway(config: AIClientConfig, session_id: str) -> tuple[AsyncOpenAI, str]:
    """Reach an OpenAI-compatible gateway that keys its state off a session id.

    Everything about which gateway comes from config: there is no default
    endpoint, and the session id rides along on every request.
    """
    model = config.model or os.environ.get("GATEWAY_MODEL")
    if not model:
        raise ValueError(
            "The gateway provider needs a model "
            "(server.ai_client.model or the GATEWAY_MODEL env var)."
        )

    base_url = config.base_url or os.environ.get("GATEWAY_BASE_URL")
    if not base_url:
        raise ValueError(
            "The gateway provider needs an endpoint "
            "(server.ai_client.base_url or the GATEWAY_BASE_URL env var)."
        )

    api_key = config.api_key or os.environ.get("GATEWAY_API_KEY")
    if not api_key:
        raise ValueError(
            "The gateway provider needs an API key "
            "(server.ai_client.api_key or the GATEWAY_API_KEY env var)."
        )

    client = AsyncOpenAI(max_retries=5, base_url=base_url, api_key=api_key)
    client.responses.create = partial(  # ty:ignore[invalid-assignment]
        client.responses.create,
        extra_body={
            "session_id": session_id,
            "strict_session": bool(config.strict_session),
        },
    )

    return client, model
