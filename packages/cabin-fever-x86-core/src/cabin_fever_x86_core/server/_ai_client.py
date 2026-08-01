"""Factory for creating an AsyncOpenAI client from an AIClientConfig."""

import os
from functools import partial
from typing import assert_never

from openai import AsyncOpenAI

from cabin_fever_x86_core.config import AIClientConfig

# The standard Azure AI scope. Any other endpoint — a research gateway, a
# private deployment — sets its own through config or AZURE_SCOPE.
AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"


def create_client(config: AIClientConfig, session_id: str) -> tuple[AsyncOpenAI, str]:
    """Create an AsyncOpenAI client and return (client, model) based on config.provider.

    For each setting the resolution order is: config field → env var → built-in default.
    """
    if config.provider == "openai":
        return _create_openai(config)
    elif config.provider == "azure":
        return _create_azure(config)
    elif config.provider == "gateway":
        return _create_gateway(config, session_id=session_id)
    else:
        assert_never(config.provider)


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
