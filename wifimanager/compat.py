"""Small compatibility layer so the library runs on MicroPython and on CPython (host tests)."""
import sys
import time

try:
    import asyncio
except ImportError:  # MicroPython < 1.21
    import uasyncio as asyncio

try:
    import json
except ImportError:
    import ujson as json

IS_MICROPYTHON = sys.implementation.name == "micropython"

if IS_MICROPYTHON:
    ticks_ms = time.ticks_ms
    ticks_diff = time.ticks_diff
    ticks_add = time.ticks_add
    sleep_ms_blocking = time.sleep_ms

    def run_for(ms):
        """Run the event loop for `ms` milliseconds (used by the blocking API to serve the portal)."""
        asyncio.run(sleep_ms(ms))

    def run_until_complete(coro):
        return asyncio.run(coro)

    def create_task(coro):
        return asyncio.create_task(coro)

else:
    def ticks_ms():
        return int(time.monotonic() * 1000)

    def ticks_diff(a, b):
        return a - b

    def ticks_add(a, b):
        return a + b

    def sleep_ms_blocking(ms):
        time.sleep(ms / 1000)

    _loop = None

    def _get_loop():
        global _loop
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        return _loop

    def run_for(ms):
        _get_loop().run_until_complete(sleep_ms(ms))

    def run_until_complete(coro):
        return _get_loop().run_until_complete(coro)

    def create_task(coro):
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            return _get_loop().create_task(coro)


async def sleep_ms(ms):
    if hasattr(asyncio, "sleep_ms"):
        await asyncio.sleep_ms(ms)
    else:
        await asyncio.sleep(ms / 1000)


def is_coroutine(obj):
    """True for the object returned by calling an `async def` function."""
    return hasattr(obj, "send") and hasattr(obj, "throw")


def exc_text(e):
    return "%s: %s" % (type(e).__name__, e)
