"""Shared configuration loading for the server and clients.

Config files are YAML, with ``${ENV_VAR_NAME}`` references in any string value
resolved against the process environment. A reference to an unset variable
leaves nothing behind: the key is treated as absent and the built-in default
applies, so ``api_key: ${OPENAI_API_KEY}`` is harmless when the variable is
not exported.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path("config.yaml")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

Provider = Literal["openai", "azure", "gateway"]

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Port = Annotated[int, Field(ge=1, le=65535)]


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or has bad values."""


class _Section(BaseModel):
    """Rejects unknown keys, so a typo is reported instead of ignored."""

    model_config = ConfigDict(extra="forbid")


class ClientConfig(_Section):
    """Where the clients connect, and the credentials they need."""

    host: str = DEFAULT_HOST
    port: Port = DEFAULT_PORT
    elevenlabs_api_key: str | None = None


class AIClientConfig(_Section):
    """Which model the server's companion runs on, and how to reach it.

    Every field is optional; :func:`cabin_fever_x86_core.server.create_client` falls
    back to environment variables and then to built-in defaults.
    """

    provider: Provider = "openai"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    #: The token scope for the providers that authenticate with Azure
    #: credentials rather than a key.
    scope: str | None = None
    #: Whether a gateway should tie requests strictly to the session id.
    strict_session: bool = True


class CabinEventsConfig(_Section):
    """How often the cabin interrupts the game.

    Each wait is drawn at random from this range, in seconds. Set them equal
    for a fixed cadence.
    """

    min_delay: Annotated[float, Field(gt=0)] = 90.0
    max_delay: Annotated[float, Field(gt=0)] = 240.0

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.min_delay > self.max_delay:
            raise ValueError(
                f"min_delay ({self.min_delay:g}) must not exceed max_delay ({self.max_delay:g})"
            )
        return self


class ServerConfig(_Section):
    """Where the server listens, and what drives the companion."""

    interface: str = DEFAULT_HOST
    port: Port = DEFAULT_PORT
    ai_client: AIClientConfig = AIClientConfig()
    cabin_events: CabinEventsConfig = CabinEventsConfig()
    #: Once a request comes back having used this many tokens, the conversation
    #: is summarised and started over from the summary. Read after the fact, so
    #: leave room for one more turn between here and the model's context limit.
    compaction_threshold: Annotated[int, Field(gt=0)] = 140_000


class Config(_Section):
    """Everything both halves of the game read at startup."""

    client: ClientConfig = ClientConfig()
    server: ServerConfig = ServerConfig()


def _resolve(value: Any) -> Any:
    """Expand ``${ENV_VAR_NAME}`` references, dropping keys left with nothing."""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, dict):
        resolved = {key: _resolve(item) for key, item in value.items()}
        return {key: item for key, item in resolved.items() if item not in (None, "")}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load a config file, falling back to defaults for anything unspecified.

    A missing file is only an error when *path* was given explicitly; the
    default ``config.yaml`` is optional.
    """
    explicit = path is not None
    config_path = Path(path) if explicit else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {config_path}")
        return Config()

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: could not parse YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: could not read file: {exc}") from exc

    try:
        return Config.model_validate(_resolve(raw))
    except ValidationError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
