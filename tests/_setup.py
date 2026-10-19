"""Import this first in every test module: puts the fakes and the library on sys.path."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, "fakes"), os.path.dirname(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)
