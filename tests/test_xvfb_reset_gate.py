import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from scripts import xvfb_reset_gate as g
from scripts import private_xvfb_noreset as display


class ResetGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (g.OLD_RUN/'status.json').exists():
            raise unittest.SkipTest('M3R evidence unavailable')
        cls.frozen, cls.inputs, cls.accepted, cls.paths, cls.review = g.review()

    def test_all_401_batches_raw_audited_without_promotion(self):
        self.assertEqual(len(self.accepted), 401)
        self.assertEqual(len({r['seed'] for r in self.accepted}), 401)
        self.assertEqual(self.review['combined_independent_batches'], 1445)
        self.assertEqual(self.review['combined_complete_points'], 350)
        self.assertEqual(self.review['remaining_m3_batches'], 859)
        self.assertEqual(self.review['precision_flags'], 47)
        self.assertNotIn(g.FAILED, {r['name'] for r in self.accepted})

    def test_noreset_keeps_auth_allocation_and_no_tcp(self):
        command = display.server_command(7, Path('/tmp/test-authority'))
        self.assertIn('-noreset', command)
        self.assertEqual(command[1:3], ['-displayfd', '7'])
        self.assertEqual(command[6:10], ['-nolisten', 'tcp', '-auth', '/tmp/test-authority'])
        self.assertNotIn('-ac', command)

    def test_exact_inputs_and_diagnostic_accounting(self):
        with tempfile.TemporaryDirectory() as td, patch.object(g, 'review', return_value=(self.frozen, self.inputs, self.accepted, set(self.paths), self.review)):
            plan, inputs = g.prepare(Path(td))
            self.assertEqual(plan['new_independent_production_histories'], 0)
            self.assertEqual(plan['diagnostic_histories'], 100)
            self.assertEqual(plan['budget_seconds'], 900)
            self.assertGreater(plan['inherited_used_seconds'], 8*3600)
            cases = {c['name']: c for c in self.frozen['cases']}
            for case in plan['cases']:
                self.assertTrue(case['diagnostic'])
                self.assertEqual(case['seed'], cases[case['name']]['seed'])
                self.assertEqual(inputs[case['name']], self.inputs[case['name']])
            frozen_path = Path(td)/'plan.json'
            changed = copy.deepcopy(plan); changed['budget_seconds'] = 1800
            frozen_path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, 'Frozen reset gate changed'):
                g.prepare(Path(td))

    def test_raw_tamper_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'raw'; path.write_bytes(b'original')
            manifest = {str(path): g.p.history.actual_sha256(path)}
            g.verify_hashes(manifest)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Frozen evidence changed'):
                g.verify_hashes(manifest)

    def test_failed_gate_cannot_be_reused_as_production(self):
        case = next(c for c in self.frozen['cases'] if c['name'] == g.FAILED)
        row = dict(case, status='FAILED', display_startup={'reset_on_last_client': False})
        with patch.object(g.p, 'audit_case'):
            with self.assertRaisesRegex(ValueError, 'Replay or noreset'):
                g.audit_replay(case, row, self.inputs[g.FAILED])

    def test_reference_requires_identical_outputs_and_new_display_evidence(self):
        case = dict(next(c for c in self.frozen['cases'] if c['name'] == g.REFERENCE), replay_reference='reference')
        row = dict(status='COMPLETE', display_startup={'reset_on_last_client': False, 'helper': str(Path(display.__file__))})
        with patch.object(g.p, 'audit_case'):
            with self.assertRaisesRegex(ValueError, 'not byte-identical'):
                g.audit_replay(case, row, self.inputs[g.REFERENCE])
            row['replay_passed'] = True
            g.audit_replay(case, row, self.inputs[g.REFERENCE])
            row['display_startup']['reset_on_last_client'] = True
            with self.assertRaisesRegex(ValueError, 'noreset lifecycle'):
                g.audit_replay(case, row, self.inputs[g.REFERENCE])

    def test_unknown_worker_command_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected worker command'):
            g.launch(['wine', 'TRIM.exe'], Path('/tmp/runtime'), 30)


class ResetWrapperTests(unittest.TestCase):
    def test_live_noreset_authentication_reconnect_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            with display.private_display(Path(td)) as (env, server, metadata):
                self.assertFalse(metadata['reset_on_last_client'])
                auth = Path(env['XAUTHORITY'])
                self.assertEqual(auth.stat().st_mode & 0o777, 0o600)
                for _ in range(20):
                    result = subprocess.run(['xwininfo', '-root'], env=env, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0)
                empty = Path(td)/'empty'; empty.touch(mode=0o600)
                denied = subprocess.run(['xwininfo', '-root'], env=dict(env, XAUTHORITY=str(empty)), capture_output=True, timeout=5)
                self.assertNotEqual(denied.returncode, 0)
            self.assertIsNotNone(server.poll())
            self.assertFalse(auth.exists())

    def test_new_cli_exit_code_and_interrupt_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            command = [sys.executable, display.__file__, '--runtime', td, '--', sys.executable, '-c']
            result = subprocess.run(command+['raise SystemExit(7)'], capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 7, result.stderr.decode())
        with tempfile.TemporaryDirectory() as td:
            command = [sys.executable, display.__file__, '--runtime', td, '--', sys.executable, '-c', 'import time; time.sleep(60)']
            child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                metadata = Path(td)/'.display-startup.json'; deadline = time.monotonic()+10
                while not metadata.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(metadata.exists())
                pid = json.loads(metadata.read_text())['server_pid']
                child.terminate(); child.communicate(timeout=15)
                self.assertEqual(child.returncode, 130)
                self.assertFalse(Path(f'/proc/{pid}').exists())
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate(timeout=10)

    def test_context_restored_after_worker_failure(self):
        original = display.original.private_display
        with patch.object(display.original, 'run', side_effect=RuntimeError('child failed')):
            with self.assertRaisesRegex(RuntimeError, 'child failed'):
                display.run(Path('/tmp/runtime'), ['false'])
        self.assertIs(display.original.private_display, original)
