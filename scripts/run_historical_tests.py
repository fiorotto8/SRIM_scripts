#!/usr/bin/env python3
"""Run regression tests with explicit pre-upgrade source-audit resolution."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.historical_execution import historical_sources


if __name__ == '__main__':
    with historical_sources():
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
    raise SystemExit(0 if result.wasSuccessful() else 1)
