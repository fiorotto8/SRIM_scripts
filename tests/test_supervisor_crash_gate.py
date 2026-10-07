import copy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack
from scripts import supervisor_crash_gate as g


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviewed = g.review()
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        (cls.root/'crash-journal.json').write_bytes((g.RUN/'crash-journal.json').read_bytes())
        with patch.object(g, 'RUN', cls.root), patch.object(g, 'review', return_value=cls.reviewed):
            cls.plan, cls.inputs, cls.rows = g.prepare()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_saved_cohort_and_counts(self):
        review = self.plan['review']
        self.assertEqual(review['accepted_batches'], 251)
        self.assertEqual(review['interrupted_batches'], 15)
        self.assertEqual(review['unlaunched_batches'], 994)
        self.assertEqual(review['remaining_batches'], 1009)
        self.assertEqual(review['independent_batches'], 4787)
        self.assertFalse(review['root_cause_proven'])

    def test_budget_preserved_with_gap_termination_and_gate(self):
        review = self.plan['review']
        old = json.loads((g.old.RUN/'budget.json').read_text())
        self.assertGreaterEqual(review['inherited_used_seconds'], old['used_seconds']+90)
        self.assertLess(review['inherited_used_seconds']/3600+.25+
                        review['remaining_forecast']['planning_hours']+3.025, 24)

    def test_exact_low_energy_diagnostics_not_new_production(self):
        cases = self.plan['cases']
        self.assertEqual(len(cases), 15)
        self.assertEqual(self.plan['diagnostic_histories'], 750)
        self.assertEqual(self.plan['new_independent_production_histories'], 0)
        indexed = {x['name']: x for x in self.reviewed[0]['cases']}
        for case in cases:
            self.assertEqual(case['energy_kev'], 1.)
            self.assertTrue(case['diagnostic'])
            original = indexed[case['name']]
            self.assertEqual({k: v for k, v in case.items() if k not in ('diagnostic', 'replay_reference')},
                             {k: v for k, v in original.items() if k != 'diagnostic'})
            reference = Path(case['replay_reference'])
            self.assertEqual(self.inputs[case['name']], ((reference/'TRIM.IN').read_bytes(), (reference/'TRIM.DAT').read_bytes()))
        self.assertEqual({c['family'] for c in cases}, {'Ar', 'Kr', 'Xe'})
        self.assertEqual(sum(c['pressure_atm'] == .5 for c in cases), 9)
        self.assertEqual(sum(c['pressure_atm'] == 3. for c in cases), 6)

    def test_preserves_all_interrupted_attempts_and_live_sources(self):
        paths = self.plan['input_sha256']
        for directory in (g.old.RUN/'cases').iterdir():
            if not (directory/'result.json').exists():
                for path in directory.rglob('*'):
                    if path.is_file():
                        self.assertIn(str(path), paths)
        self.assertIn(str(Path(sys.executable).resolve()), paths)
        self.assertIn(str(g.ROOT/'scripts/supervisor_crash_gate.sh'), paths)

    def test_modified_gate_is_rejected(self):
        changed = copy.deepcopy(self.plan); changed['diagnostic_histories'] = 751
        path = self.root/'plan.json'; path.write_text(json.dumps(changed))
        try:
            with patch.object(g, 'RUN', self.root), patch.object(g, 'review', return_value=self.reviewed):
                with self.assertRaisesRegex(ValueError, 'Frozen crash gate'):
                    g.prepare()
        finally:
            path.write_text(json.dumps(self.plan))

    def test_replay_requires_diagnostic_and_owned_noreset_attempt(self):
        with patch.object(g.old, 'original_audit'):
            with self.assertRaisesRegex(ValueError, 'replay integrity'):
                g.audit_replay({'diagnostic': False}, {}, ())
            with self.assertRaisesRegex(ValueError, 'replay integrity'):
                g.audit_replay({'diagnostic': True}, {'replay_passed': True, 'attempt': '/tmp/not-this-gate'}, ())


class ContainmentTests(unittest.TestCase):
    def test_native_child_fault_is_visible_and_parent_survives(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td)/'crash.log'
            command = [sys.executable, '-X', 'faulthandler', '-c',
                       'import os,signal,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.kill(os.getpid(),signal.SIGSEGV)']
            process, deadline = g.spawn_checked(command, log, 10)
            self.assertEqual(process.wait(timeout=10), -signal.SIGSEGV)
            self.assertIn('Fatal Python error: Segmentation fault', log.read_text())
            self.assertGreater(deadline, 0)

    def test_regular_child_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            process, _ = g.spawn_checked([sys.executable, '-c', 'raise SystemExit(7)'], Path(td)/'error.log', 10)
            self.assertEqual(process.wait(timeout=10), 7)

    def test_stale_running_status_is_finalized_after_crash(self):
        with tempfile.TemporaryDirectory() as td, patch.object(g, 'RUN', Path(td)), \
                patch.object(sys, 'argv', ['gate', 'finalize']), patch.dict(os.environ, SERVICE_RESULT='core-dump', EXIT_STATUS='SEGV'):
            path = Path(td)/'status.json'
            path.write_text(json.dumps(dict(status='RUNNING', started_at='2026-10-07T00:00:00+02:00')))
            self.assertEqual(g.main(), 0)
            state = json.loads(path.read_text())
            self.assertEqual(state['status'], 'NEEDS_ATTENTION')
            self.assertEqual(state['service_exit']['exit_status'], 'SEGV')
            self.assertGreater(state['wall_seconds'], 0)

    def test_finalizer_preserves_successful_review_status(self):
        with tempfile.TemporaryDirectory() as td, patch.object(g, 'RUN', Path(td)), patch.object(sys, 'argv', ['gate', 'finalize']):
            path = Path(td)/'status.json'; content = json.dumps(dict(status='REVIEW_REQUIRED'))
            path.write_text(content)
            self.assertEqual(g.main(), 0)
            self.assertEqual(path.read_text(), content)

    def test_run_requires_bounded_service(self):
        with patch.object(g, 'resource_sample', return_value={'cgroup': '/not-the-bounded-service'}):
            with self.assertRaisesRegex(RuntimeError, 'bounded'):
                g.run()

    def exercise_failed_stress(self, timed_out):
        class FailedChild:
            pid = 123456789
            def poll(self): return None if timed_out else 7
            def terminate(self): pass
            def wait(self, timeout=None): return -15
        original_spawn = g.spawn_checked
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td); group = root/g.UNIT; group.mkdir()
            (group/'memory.max').write_text(str(32*g.GIB)); (group/'memory.swap.max').write_text('0')
            stack.enter_context(patch.object(g, 'RUN', root))
            stack.enter_context(patch.object(g, 'resource_sample', return_value={'cgroup': str(group)}))
            stack.enter_context(patch.object(g.os, 'sched_getaffinity', return_value={0}))
            stack.enter_context(patch.object(g, 'memory_info', return_value={'MemAvailable': 40*g.GIB}))
            stack.enter_context(patch.object(g.signal, 'signal'))
            stack.enter_context(patch.object(g, 'prepare', return_value=({}, {}, [])))
            def spawn(command, log, timeout):
                if log.name == 'fault-injection.log':
                    return original_spawn(command, log, timeout)
                return FailedChild(), -1
            stack.enter_context(patch.object(g, 'spawn_checked', side_effect=spawn))
            runtimes = stack.enter_context(patch.object(g.scan, 'prepare_runtimes', side_effect=AssertionError('Replays must stay held')))
            self.assertEqual(g.run(), 1)
            self.assertEqual(json.loads((root/'status.json').read_text())['status'], 'NEEDS_ATTENTION')
            runtimes.assert_not_called()
            self.assertFalse((root/'completion.json').exists())

    def test_failed_child_holds_replay_stage(self):
        self.exercise_failed_stress(False)

    def test_timed_out_child_holds_replay_stage(self):
        self.exercise_failed_stress(True)


if __name__ == '__main__':
    unittest.main()
