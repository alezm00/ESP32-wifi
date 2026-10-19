"""Tiny logger: console + RAM ring buffer (shown in the web page) + optional rotating file."""
import os
import time

from . import compat

DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40

_NAMES = {DEBUG: "DEBUG", INFO: "INFO", WARNING: "WARN", ERROR: "ERROR"}
_BY_NAME = {"DEBUG": DEBUG, "INFO": INFO, "WARNING": WARNING, "WARN": WARNING, "ERROR": ERROR}


def level_from(value):
    if isinstance(value, str):
        try:
            return _BY_NAME[value.upper()]
        except KeyError:
            raise ValueError("unknown log level %r" % value)
    return value


class Logger:
    def __init__(self, level=INFO, buffer=60, file=None, max_size=8192, tag="wifi", console=True):
        self.level = level_from(level)
        self.buffer_size = buffer
        self.file = file
        self.max_size = max_size
        self.tag = tag
        self.console = console
        self._lines = []
        self._size = None

    def _stamp(self):
        try:
            t = time.localtime()
            if t[0] >= 2024:  # RTC has been set (NTP or manually)
                return "%04d-%02d-%02d %02d:%02d:%02d" % t[:6]
        except Exception:
            pass
        ms = compat.ticks_ms()
        return "%8.1fs" % (ms / 1000)

    def log(self, level, msg, *args):
        if level < self.level:
            return
        if args:
            try:
                msg = msg % args
            except Exception:
                msg = "%s %r" % (msg, args)
        line = "[%s] %-5s %s: %s" % (self._stamp(), _NAMES.get(level, level), self.tag, msg)
        if self.console:
            print(line)
        if self.buffer_size:
            self._lines.append(line)
            if len(self._lines) > self.buffer_size:
                self._lines.pop(0)
        if self.file:
            self._write_file(line)

    def debug(self, msg, *args):
        self.log(DEBUG, msg, *args)

    def info(self, msg, *args):
        self.log(INFO, msg, *args)

    def warning(self, msg, *args):
        self.log(WARNING, msg, *args)

    def error(self, msg, *args):
        self.log(ERROR, msg, *args)

    def tail(self, n=None):
        """Last `n` buffered lines (all when `n` is None)."""
        if n is None or n >= len(self._lines):
            return list(self._lines)
        return self._lines[-n:]

    def _write_file(self, line):
        try:
            if self._size is None:
                try:
                    self._size = os.stat(self.file)[6]
                except OSError:
                    self._size = 0
            if self.max_size and self._size >= self.max_size:
                self._rotate()
            with open(self.file, "a") as f:
                f.write(line + "\n")
            self._size += len(line) + 1
        except Exception:
            self.file = None  # never let logging break the app; stop trying
            print("wifi: log file disabled (write error)")

    def _rotate(self):
        old = self.file + ".1"
        try:
            os.remove(old)
        except OSError:
            pass
        os.rename(self.file, old)
        self._size = 0
