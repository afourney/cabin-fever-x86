"""Cancellation boundaries for operations that must finish before releasing state."""

import asyncio
from collections.abc import Awaitable


async def finish_on_cancel[T](operation: Awaitable[T]) -> T:
    """Wait for an operation to settle even if its caller is cancelled.

    Keep cancellation outside state writes and resource cleanup. Repeated cancellation
    requests must not abandon a thread or cleanup task that is still using a session.
    """
    task = asyncio.ensure_future(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 -- task.result() below re-raises the stored failure.
            break
    if cancelled:
        if not task.cancelled():
            task.exception()  # Retrieve a failure without swallowing the caller's cancellation.
        raise asyncio.CancelledError
    return task.result()
