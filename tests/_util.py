"""Small helpers the MicroPython test runner lacks: temporary directories and async tests."""
import asyncio
import os
import random


def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def tmpdir():
    path = "/tmp/wm-test-%08x" % random.getrandbits(32)
    os.mkdir(path)
    return path


def rmtree(path):
    try:
        entries = list(os.ilistdir(path))
    except OSError:
        return
    for name, kind, *_ in entries:
        full = path + "/" + name
        if kind == 0x4000:
            rmtree(full)
        else:
            os.remove(full)
    os.rmdir(path)


def async_test(fn):
    """Run `async def test_x(self)` inside asyncio.run(), between asyncSetUp() and asyncTearDown()."""
    def run(self):
        async def main():
            setup = getattr(self, "asyncSetUp", None)
            if setup is not None:
                await setup()
            try:
                await fn(self)
            finally:
                teardown = getattr(self, "asyncTearDown", None)
                if teardown is not None:
                    await teardown()
        asyncio.run(main())
    return run
