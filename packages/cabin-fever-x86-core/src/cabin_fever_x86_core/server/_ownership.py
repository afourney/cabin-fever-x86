"""Serialize game lifetimes across all connections to one server process."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from uuid import UUID

from websockets.asyncio.server import ServerConnection

from cabin_fever_x86_core.async_utils import finish_on_cancel
from cabin_fever_x86_core.config import ServerConfig
from cabin_fever_x86_core.messages import SESSION_REPLACED, AssistantMessage
from cabin_fever_x86_core.server._game import Game, SendCallback

logger = logging.getLogger(__name__)
CLEANUP_TIMEOUT = 30


class OwnershipError(Exception):
    """An ownership request that cannot safely start a game."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Owner:
    """One connection's game, including cleanup after the connection is retired."""

    game: Game
    connection: ServerConnection
    token: str = field(repr=False)
    active: bool = True
    cleanup: asyncio.Task[None] | None = None

    def begin_stop(self) -> asyncio.Task[None]:
        """Start cleanup exactly once; retain its outcome even if a waiter leaves."""
        self.active = False
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self.game.__aexit__(None, None, None))
        return self.cleanup

    async def stop(self) -> None:
        """Stop exactly once; a failed cleanup continues to block replacement."""
        await finish_on_cancel(self.begin_stop())


@dataclass
class Slot:
    """Retain token history even while no game is running."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    token: str | None = field(default=None, repr=False)
    owner: Owner | None = None


class SessionRegistry:
    """One owner per user/session, shared by every connection to the server."""

    def __init__(self) -> None:
        self.slots: dict[tuple[str, UUID], Slot] = {}
        self.closing: set[asyncio.Task[None]] = set()

    def _displace(self, owner: Owner) -> None:
        owner.active = False

        async def close_transport() -> None:
            try:
                await owner.connection.close(
                    code=SESSION_REPLACED, reason="Session taken over elsewhere"
                )
            except Exception:
                logger.exception("Could not close displaced connection")

        # Never wait for an unreachable client to acknowledge the close before cleanup.
        task = asyncio.create_task(close_transport())
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)

    async def acquire(
        self,
        user_id: str,
        session_id: UUID,
        connection: ServerConnection,
        config: ServerConfig,
        send: SendCallback,
        *,
        mode: str,
        token: str | None = None,
    ) -> Owner:
        """Retire the old game fully before starting its replacement."""
        slot = self.slots.setdefault((user_id, session_id), Slot())
        async with slot.lock:
            if mode == "recover":
                if (
                    token is None
                    or slot.token is None
                    or not secrets.compare_digest(token.encode(), slot.token.encode())
                ):
                    raise OwnershipError(
                        "Ownership expired. Explicitly resume the session.", "resume_required"
                    )
            elif mode == "legacy" and slot.owner is not None:
                raise OwnershipError(
                    "Session is already in use. Upgrade the client to take over.", "session_in_use"
                )
            else:
                slot.token = secrets.token_urlsafe(32)

            if slot.owner is not None:
                self._displace(slot.owner)
                cleanup = slot.owner.begin_stop()
                done, _ = await finish_on_cancel(asyncio.wait({cleanup}, timeout=CLEANUP_TIMEOUT))
                if not done:
                    raise OwnershipError(
                        "Previous game is still stopping. Please try again.", "cleanup_pending"
                    )
                try:
                    cleanup.result()
                except Exception as exc:
                    raise OwnershipError(
                        "Previous game cleanup failed; restart the server before resuming.",
                        "cleanup_failed",
                    ) from exc
                slot.owner = None

            async def deliver(message: AssistantMessage) -> None:
                if owner.active:
                    await send(message)

            owner = Owner(
                Game(config, deliver, session_id, user_id=user_id), connection, slot.token
            )
            slot.owner = owner
            try:
                # Startup can allocate resources or write the journal in worker threads.
                await finish_on_cancel(owner.game.__aenter__())
            except BaseException:
                await owner.stop()
                slot.owner = None
                raise
            return owner

    async def release(self, user_id: str, owner: Owner) -> None:
        """Finish cleanup independently of the lock a takeover may be holding."""
        await owner.stop()
        slot = self.slots[(user_id, owner.game.session_id)]
        async with slot.lock:
            if slot.owner is owner:
                slot.owner = None

    async def close(self) -> None:
        """Drain games and transport close tasks during server shutdown."""
        await asyncio.gather(
            *(slot.owner.stop() for slot in self.slots.values() if slot.owner),
            return_exceptions=True,
        )
        if self.closing:
            await asyncio.gather(*self.closing, return_exceptions=True)
