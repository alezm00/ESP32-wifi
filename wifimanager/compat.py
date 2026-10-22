"""Thin indirection over `time` and `asyncio` (MicroPython v1.29).

Built-in modules cannot be patched, these names can: the tests swap them for a virtual clock.
"""
import asyncio
import time

ticks_ms = time.ticks_ms
ticks_diff = time.ticks_diff
ticks_add = time.ticks_add
sleep_ms_blocking = time.sleep_ms
sleep_ms = asyncio.sleep_ms
create_task = asyncio.create_task
run_until_complete = asyncio.run


async def _pause(ms):
    await asyncio.sleep_ms(ms)  # sleep_ms() itself is not a coroutine: asyncio.run() refuses it


def run_for(ms):
    """Run the event loop for `ms` milliseconds (used by the blocking API to serve the portal)."""
    asyncio.run(_pause(ms))


def is_coroutine(obj):
    """True for the object returned by calling an `async def` function."""
    return hasattr(obj, "send") and hasattr(obj, "throw")


def exc_text(e):
    return "%s: %s" % (type(e).__name__, e)
