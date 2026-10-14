"""
Regression test: prove the OLD ask_agent code blocked the event loop.

This test shows that a synchronous (non-awaiting) async function starves
the event loop, exactly reproducing the production failure mode where
Discord heartbeats were missed and all slash commands returned
"Unknown interaction" errors.
"""

import asyncio
import time


def test_sync_coroutine_blocks_event_loop():
    """Demonstrate that an async-def with sync work blocks the loop."""

    async def blocking_coroutine():
        """Simulates the OLD ask_agent: async def with sync sleep (no await)."""
        time.sleep(0.15)
        return "done"

    heartbeat_count = 0

    async def heartbeat():
        nonlocal heartbeat_count
        for _ in range(20):
            await asyncio.sleep(0.01)
            heartbeat_count += 1

    async def run():
        nonlocal heartbeat_count
        task = asyncio.create_task(heartbeat())
        _result = await blocking_coroutine()
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return heartbeat_count

    beats = asyncio.run(run())
    assert beats <= 1, (
        f"Expected ≤1 heartbeat during blocking coroutine, got {beats}. "
        "This test verifies the OLD broken behavior."
    )


def test_async_coroutine_does_not_block_event_loop():
    """Demonstrate that a properly async function lets the loop breathe."""

    async def non_blocking_coroutine():
        """Simulates the FIXED ask_agent: async def with real awaits."""
        await asyncio.sleep(0.15)
        return "done"

    heartbeat_count = 0

    async def heartbeat():
        nonlocal heartbeat_count
        for _ in range(20):
            await asyncio.sleep(0.01)
            heartbeat_count += 1

    async def run():
        nonlocal heartbeat_count
        task = asyncio.create_task(heartbeat())
        _result = await non_blocking_coroutine()
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return heartbeat_count

    beats = asyncio.run(run())
    assert beats >= 5, (
        f"Expected ≥5 heartbeats during non-blocking coroutine, got {beats}. "
        "The fixed ask_agent must yield to the event loop."
    )
