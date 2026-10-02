"""Versioned historical Xvfb audit after the documented post-P2b OS upgrade.

Never substitutes bytes during execution. Only legacy hash checks inside the
explicit context inspect the recovered, hash-identical historical executable.
New execution manifests must use actual_sha256 and pin the current executable.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'runs/xvfb_upgrade_gate/upgrade-evidence.json'
HISTORICAL = ROOT / 'runs/xvfb_upgrade_gate/old-package/usr/bin/Xvfb'
LIVE = Path('/usr/bin/Xvfb')
OLD_SHA = '2c7f5a9534410fed5092d782a69ca7ffd9fce80e98b81ffe4944d703dd11d3b1'
NEW_SHA = '6b27845931670c8c9b3ce7423696a452ce3cab1c7f316ce5f7ea7f1297fa2f84'


def actual_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence():
    record = json.loads(EVIDENCE.read_text())
    if record['historical_sha256'] != OLD_SHA or actual_sha256(HISTORICAL) != OLD_SHA:
        raise ValueError('Recovered historical Xvfb is not hash-identical')
    if record['current_sha256'] != NEW_SHA or actual_sha256(LIVE) != NEW_SHA:
        raise ValueError('Current Xvfb changed; a separate environment review is required')
    completion = json.loads((ROOT / 'runs/noble_cf4_high_energy_pressure_batches/completion.json').read_text())
    if completion['status'] != 'REVIEW_REQUIRED' or not completion['source_unchanged']:
        raise ValueError('Pre-upgrade P2b completion evidence missing')
    if datetime.fromisoformat(record['upgrade_at']) <= datetime.fromisoformat(completion['updated_at']):
        raise ValueError('Upgrade was not after the completed historical campaign')
    return record


@contextmanager
def historical_sources():
    """Audit pre-upgrade stages, fail closed on any unrelated hash change."""
    import srim_scan as scan
    record = evidence(); original = scan.sha256_file
    def historical_hash(path):
        if Path(path).resolve() == LIVE:
            return actual_sha256(HISTORICAL)
        return original(path)
    scan.sha256_file = historical_hash
    try:
        yield record
    finally:
        scan.sha256_file = original
