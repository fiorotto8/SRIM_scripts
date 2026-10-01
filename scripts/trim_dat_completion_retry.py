#!/usr/bin/env python3
"""Opt-in mode-5 completion inspection with bounded X11 race recovery.

Only BadWindow/BadDrawable failures from xwininfo enumeration or detail probes
are retried. Every attempt starts with the original error watchdog. Completion
acknowledgement retains the established exact allowlist and is attempted once.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import trim_dat_completion as legacy
from scripts.staged_scan import write_json

original = legacy
TITLE = legacy.TITLE
DELAYS = (0.25, 0.5)
RACE_ERROR = re.compile(
    r'(?:X Error of failed request:\s*(?:BadWindow|BadDrawable|Bad Window|Bad Drawable)\b'
    r'|^X Error:\s*(?:3:\s*Bad Window|9:\s*Bad Drawable)\s*$)',
    re.MULTILINE,
)


def _error_text(exc):
    stderr = getattr(exc, 'stderr', '') or ''
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', errors='replace')
    return str(stderr)[-4000:]


def _window_race(exc):
    return isinstance(exc, subprocess.CalledProcessError) and bool(RACE_ERROR.search(_error_text(exc)))


class CompletionChecker(legacy.CompletionChecker):
    def __init__(self, runtime, original):
        super().__init__(runtime, original)
        self._ack_attempted = False
        self._last_tree = ''
        self._probe_records = []
        self._record(None)

    def _record(self, record):
        write_json(self.runtime / '.completion-probes.json', {
            'version': 1, 'display': os.environ.get('DISPLAY', ''),
            'helper': str(Path(__file__).resolve()),
            'original_helper': str(Path(legacy.__file__).resolve()),
            'policy': 'three attempts for xwininfo BadWindow/BadDrawable; no repeated acknowledgement',
            'inspections': self._probe_records,
        })

    def _fail(self, message, exc=None):
        detail = f'; stderr={_error_text(exc)!r}' if exc is not None else ''
        raise RuntimeError(f'TRIM_WATCHDOG_ERROR: {message}{detail}') from exc

    def _probe(self, command):
        return subprocess.run(command, capture_output=True, text=True, timeout=5, check=True).stdout

    def _check_acknowledged(self):
        if self.acknowledged_at is None:
            return False
        if time.monotonic() - self.acknowledged_at > 20:
            # The saved tree is evidence of the acknowledged window. A new
            # enumeration could race normal exit and cannot extend this deadline.
            try:
                legacy.capture(self.runtime, 'post-completion-stall', self._last_tree)
            except Exception as exc:
                print(f'Completion stall evidence capture failed: {exc}', file=sys.stderr, flush=True)
            self._fail('TRIM did not exit within 20 seconds after completion acknowledgement')
        return True

    def __call__(self):
        record = None
        for attempt in range(3):
            try:
                errors = self.original()
            except (OSError, subprocess.SubprocessError) as exc:
                self._fail('original error watchdog cannot inspect the worker display', exc)
            if errors:
                if record is not None:
                    record.update(status='error_dialog', attempts=attempt + 1)
                    self._record(record)
                return errors
            # The original watchdog remains active while TRIM writes its final
            # outputs. No secondary completion query or key is needed after ACK.
            if self._check_acknowledged():
                return []
            if self._ack_attempted:
                self._fail('completion acknowledgement was already attempted; no second key permitted')
            try:
                tree = self._probe(['xwininfo', '-root', '-tree'])
                windows = legacy.completion_candidates(tree)
                if len(windows) > 1:
                    self._fail('Ambiguous completion windows; no action')
                details = self._probe(['xwininfo', '-id', windows[0]]) if windows else ''
            except (OSError, subprocess.SubprocessError) as exc:
                if record is None:
                    record = {'status': 'retrying', 'epoch': time.time(), 'failures': []}
                    self._probe_records.append(record)
                record['failures'].append({
                    'attempt': attempt + 1, 'command': getattr(exc, 'cmd', None),
                    'returncode': getattr(exc, 'returncode', None),
                    'stderr': _error_text(exc), 'error': str(exc),
                })
                retry = _window_race(exc) and attempt < 2
                record.update(status='retrying' if retry else 'failed', attempts=attempt + 1)
                self._record(record)
                print(f'Completion probe {attempt + 1}/3 failed: {exc}; stderr={_error_text(exc)!r}',
                      file=sys.stderr, flush=True)
                if not retry:
                    self._fail('cannot inspect mode-5 completion display', exc)
                time.sleep(DELAYS[attempt])
                continue
            if record is not None:
                record.update(status='recovered', attempts=attempt + 1)
                self._record(record)
                print(f'Completion probe recovered on attempt {attempt + 1}/3', file=sys.stderr, flush=True)
            self._last_tree = tree
            if not windows or 'Map State: IsViewable' not in details:
                return []
            try:
                legacy.capture(self.runtime, 'normal-completion', tree)
            except Exception as exc:
                self._fail('cannot capture mode-5 completion evidence', exc)
            values = dict(title=TITLE, window=windows[0],
                          action='one Return to exact visible completion window',
                          status='attempting', epoch=time.time())
            write_json(self.runtime / 'completion-ack.json', values)
            self._ack_attempted = True
            try:
                legacy.acknowledge(windows[0])
            except Exception as exc:
                self._fail('completion acknowledgement failed; no retry permitted', exc)
            self.acknowledged_at = time.monotonic()
            values.update(status='sent', epoch=time.time())
            write_json(self.runtime / 'completion-ack.json', values)
            return []
        raise AssertionError('unreachable')


def run(runtime: Path, description: str):
    import pysrim_runner as runner
    from scripts.binding_probe import ArchivedInput

    lines = (runtime / 'TRIM.IN').read_text(encoding='ascii').splitlines()
    mode = lines[next(i for i, line in enumerate(lines) if line.startswith('Cascades(')) + 1].split()[0]
    if mode != '5':
        raise ValueError('Completion acknowledgement is mode-5 only')
    original = runner.check_trim_dialogs
    checker = CompletionChecker(runtime, original)
    runner.check_trim_dialogs = checker
    try:
        runner.configure_recoil_capacity(runtime)
        runner.monitored_trim_run(ArchivedInput(), runtime, dict(description=description))
        if checker.acknowledged_at is None:
            raise RuntimeError('Mode-5 exited without expected completion evidence')
    finally:
        runner.check_trim_dialogs = original


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    run(args.runtime, args.name)
