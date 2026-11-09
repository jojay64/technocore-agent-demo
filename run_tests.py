"""Run every offline Python test suite, one directory at a time.

Each service lives in its own folder and its modules import each other by
plain name, so each folder is discovered with itself as the top-level dir.
Run from the repository root (state/identity paths are relative to it).
"""
import sys
import unittest
from pathlib import Path

SUITES = ("pulse", "tclk", "closecall")


def main() -> int:
    root = Path(__file__).resolve().parent
    verbosity = 2 if "-v" in sys.argv[1:] else 1
    ok = True
    for name in SUITES:
        suite = unittest.defaultTestLoader.discover(
            str(root / name), pattern="test_*.py", top_level_dir=str(root / name)
        )
        result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
        ok = ok and result.wasSuccessful()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
