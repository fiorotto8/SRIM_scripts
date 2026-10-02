#!/usr/bin/env python3
"""Run regression tests with explicit pre-upgrade source-audit resolution."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.historical_execution import historical_sources
from contextlib import nullcontext


if __name__ == '__main__':
    if (ROOT/'runs/noble_cf4_mixtures_1atm/plan.json').exists():
        from scripts.mixtures_1atm_retry import frozen_seed_context
        seed_context = frozen_seed_context()
    else:
        seed_context = nullcontext()
    with historical_sources(), seed_context:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
    raise SystemExit(0 if result.wasSuccessful() else 1)
