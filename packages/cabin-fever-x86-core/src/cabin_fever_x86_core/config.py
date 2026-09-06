"""Shared configuration loading for the server, gateways, and text client.

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
from urllib.parse import urlsplit

import yaml
from argon2 import Type, extract_parameters
from argon2.exceptions import InvalidHashError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from cabin_fever_x86_core.sessions import GUEST_USER_ID, validate_user_id

DEFAULT_CONFIG_PATH = Path("config.yaml")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

Provider = Literal["openai", "azure", "gateway"]

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

Port = Annotated[int, Field(ge=1, le=65535)]
ZelloName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or has bad values."""


class _Section(BaseModel):
    """Rejects unknown keys, so a typo is reported instead of ignored."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ClientConfig(_Section):
    """Where the text client and gateways connect, and the credentials they need."""

    host: str = DEFAULT_HOST
    port: Port = DEFAULT_PORT
    elevenlabs_api_key: str | None = None


class GuestIdentity(_Section):
    """Mark the guest user as available for anonymous access."""

    type: Literal["guest"]


class LoginIdentity(_Section):
    """Authenticate a browser callsign with a server-verified Argon2id hash."""

    type: Literal["login"]
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
    password_hash: SecretStr

    @field_validator("password_hash")
    @classmethod
    def _check_hash(cls, value: SecretStr) -> SecretStr:
        try:
            params = extract_parameters(value.get_secret_value())
            if (
                params.type != Type.ID
                or params.version != 19
                or not 8192 <= params.memory_cost <= 262144
                or not 1 <= params.time_cost <= 10
                or not 1 <= params.parallelism <= 8
                or params.salt_len < 16
                or params.hash_len < 16
            ):
                raise ValueError
        except (InvalidHashError, ValueError):
            raise ValueError("password_hash must be a supported Argon2id hash") from None
        return value


class WebGatewayConfig(_Section):
    """Browser security; HTTP is restricted to loopback unless HTTPS is configured."""

    public_origin: str | None = None
    data_dir: Path = Path("data/web_gateway")
    signing_secret: SecretStr | None = None
    session_idle_seconds: Annotated[int, Field(ge=60, le=86400)] = 3600
    session_max_seconds: Annotated[int, Field(ge=60, le=2592000)] = 604800

    @field_validator("public_origin")
    @classmethod
    def _check_origin(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "*" in parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public_origin must be an HTTPS origin, without a path")
        # Accessing port also validates its syntax and range.
        port = parsed.port
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}" + (f":{port}" if port and port != 443 else "")

    @field_validator("signing_secret")
    @classmethod
    def _check_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("signing_secret must contain at least 32 random characters")
        return value

    @model_validator(mode="after")
    def _check_lifetime(self) -> Self:
        if self.session_idle_seconds > self.session_max_seconds:
            raise ValueError("session_idle_seconds must not exceed session_max_seconds")
        return self


class TelegramIdentity(_Section):
    """Associate a verified numeric Telegram account with a user."""

    type: Literal["telegram"]
    account_id: Annotated[int, Field(gt=0)]

    @field_validator("account_id", mode="before")
    @classmethod
    def _check_account_id(cls, value: Any) -> int:
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        if type(value) is int:
            return value
        raise ValueError("Telegram account_id must be a positive integer or decimal string")


class ZelloIdentity(_Section):
    """Allow one Zello sender to contribute to a channel's shared game."""

    type: Literal["zello"]
    channel: ZelloName
    account_id: ZelloName


class UserConfig(_Section):
    """One stable user and the identities that may access its sessions."""

    user_id: Annotated[str, AfterValidator(validate_user_id)]
    identities: list[
        Annotated[
            GuestIdentity | LoginIdentity | TelegramIdentity | ZelloIdentity,
            Field(discriminator="type"),
        ]
    ]

    @model_validator(mode="after")
    def _check_guest(self) -> Self:
        if self.user_id != GUEST_USER_ID and any(
            isinstance(identity, GuestIdentity) for identity in self.identities
        ):
            raise ValueError("the guest identity is only allowed on user_id: guest")
        return self


class TelegramGatewayConfig(_Section):
    """How the optional Telegram gateway authenticates and who may use it."""

    bot_token: str | None = None
    api_id: Annotated[int, Field(gt=0)] | None = None
    api_hash: str | None = None


class ZelloGatewayConfig(_Section):
    """Credentials shared by the optional Zello channel connections."""

    credentials_file: str


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
    #: Do not draw or emit a scheduled interruption after this many seconds
    #: without a transmission from the player.
    inactivity_timeout: Annotated[float, Field(gt=0)] = 300.0

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
    #: leave room for one more round between here and the model's context limit.
    compaction_threshold: Annotated[int, Field(gt=0)] = 140_000


class LauncherConfig(_Section):
    """Settings used by the host-side sandbox launcher."""

    package_locator: str | None = None


class Config(_Section):
    """Everything both halves of the game read at startup."""

    launcher: LauncherConfig = LauncherConfig()
    client: ClientConfig = ClientConfig()
    web_gateway: WebGatewayConfig = WebGatewayConfig()
    telegram_gateway: TelegramGatewayConfig = TelegramGatewayConfig()
    zello: ZelloGatewayConfig | None = None
    server: ServerConfig = ServerConfig()
    users: list[UserConfig] = Field(
        default_factory=lambda: [
            UserConfig(user_id=GUEST_USER_ID, identities=[GuestIdentity(type="guest")])
        ]
    )

    @model_validator(mode="after")
    def _check_users(self) -> Self:
        user_ids: set[str] = set()
        identities: set[tuple[str, int | str | None, str | None]] = set()
        zello_owners: dict[str, str] = {}
        for user in self.users:
            if user.user_id in user_ids:
                raise ValueError(f"duplicate user_id: {user.user_id}")
            user_ids.add(user.user_id)
            for identity in user.identities:
                if isinstance(identity, LoginIdentity):
                    key = (identity.type, identity.username.casefold(), None)
                elif isinstance(identity, ZelloIdentity):
                    key = (identity.type, identity.account_id.casefold(), identity.channel)
                    owner = zello_owners.setdefault(identity.channel, user.user_id)
                    if owner != user.user_id:
                        raise ValueError(
                            f"Zello channel {identity.channel!r} must belong to one user; "
                            f"found {owner!r} and {user.user_id!r}"
                        )
                else:
                    key = (
                        identity.type,
                        identity.account_id if isinstance(identity, TelegramIdentity) else None,
                        None,
                    )
                if key in identities:
                    raise ValueError(f"duplicate {identity.type} identity")
                identities.add(key)
        return self

    def telegram_accounts(self) -> dict[int, str]:
        """Map verified Telegram account IDs to their configured internal users."""
        return {
            identity.account_id: user.user_id
            for user in self.users
            for identity in user.identities
            if isinstance(identity, TelegramIdentity)
        }

    def zello_access(self, channel: str) -> tuple[str, set[str]]:
        """Resolve a channel's game owner and case-insensitive contributor names."""
        accounts = {
            identity.account_id.casefold(): user.user_id
            for user in self.users
            for identity in user.identities
            if isinstance(identity, ZelloIdentity) and identity.channel == channel
        }
        if not accounts:
            raise ValueError(f"No Zello identities in users for channel {channel!r}")
        return next(iter(accounts.values())), set(accounts)

    def zello_channels(self) -> dict[str, tuple[str, set[str]]]:
        """Map configured channels to their sole owner and contributor names."""
        channels: dict[str, tuple[str, set[str]]] = {}
        for user in self.users:
            for identity in user.identities:
                if isinstance(identity, ZelloIdentity):
                    _, contributors = channels.setdefault(identity.channel, (user.user_id, set()))
                    contributors.add(identity.account_id.casefold())
        return channels


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
    except yaml.YAMLError:
        raise ConfigError(f"{config_path}: could not parse YAML") from None
    except OSError as exc:
        raise ConfigError(f"{config_path}: could not read file: {exc}") from exc

    try:
        return Config.model_validate(_resolve(raw))
    except ValidationError as exc:
        raise ConfigError(f"{config_path}: {exc}") from exc
