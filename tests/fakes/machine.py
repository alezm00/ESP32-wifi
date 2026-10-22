"""Fake `machine` module for host tests."""


class ResetCalled(BaseException):
    """machine.reset() never returns on a real board; BaseException lets it fly through `except Exception`."""


def reset():
    raise ResetCalled()


def unique_id():
    return b"\x24\x0a\xc4\x12\xab\xcd"


class Pin:
    IN = 0
    OUT = 1
    PULL_UP = 2
    PULL_DOWN = 3
    levels = {}   # pin number -> 0/1 (default 1 = not pressed for an active-low button)

    def __init__(self, num, mode=-1, pull=-1):
        self.num = num

    def value(self):
        return Pin.levels.get(self.num, 1)
