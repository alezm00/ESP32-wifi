"""Import this first in every test module: puts the fakes and the library on sys.path."""
import sys

HERE = __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
ROOT = HERE + "/.."
for p in (ROOT, HERE, HERE + "/fakes"):
    if p not in sys.path:
        sys.path.insert(0, p)
