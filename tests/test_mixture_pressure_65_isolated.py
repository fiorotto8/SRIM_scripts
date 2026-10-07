import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import mixture_pressure_65_isolated as r


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviewed = r.review.review()
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        with patch.object(r, 'RUN', cls.root), patch.object(r.review, 'review', return_value=cls.reviewed):
            cls.plan, cls.inputs, cls.baseline, cls.accepted = r.prepare()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_exact_original_cases_and_no_seed_or_physics_change(self):
        self.assertEqual(self.plan['cases'], self.reviewed[0]['cases'])
        self.assertEqual(self.plan['pressure_screen'], self.reviewed[0]['pressure_screen'])
        self.assertEqual(len(self.accepted), 251)
        self.assertEqual(len(self.baseline), 4536)
        self.assertEqual(len({r['seed'] for r in self.baseline+self.plan['cases']}), 5796)
        self.assertEqual(len(self.plan['retained_original_names']), 251)
        self.assertEqual(self.plan['remaining_batches'], 1009)
        self.assertTrue(all(not c['diagnostic'] for c in self.plan['cases']))

    def test_only_pending_exact_inputs_are_written(self):
        pending = [c for c in self.plan['cases'] if c['name'] not in self.plan['retained_original_names']]
        self.assertEqual(len(list((self.root/'inputs').iterdir())), 1009)
        for case in pending:
            actual = tuple((self.root/'inputs'/case['name']/n).read_bytes() for n in ('TRIM.IN', 'TRIM.DAT'))
            self.assertEqual(actual, self.inputs[case['name']])
            self.assertEqual(case['phase'], 'pressure_production')

    def test_full_diagnostic_duration_is_inherited_and_budget_fits(self):
        self.assertAlmostEqual(self.plan['inherited_used_seconds'], 20320+78.09599709510803)
        self.assertLess(self.plan['inherited_used_seconds']/3600+self.plan['forecast']['planning_hours']+3.025, 24)
        self.assertEqual(self.plan['recovery_review']['thermal_peak_c'], 83)
        self.assertFalse(self.plan['recovery_review']['root_cause_proven'])

    def test_manifest_pins_thermal_evidence_and_new_sources(self):
        for path in (r.g.RUN/'thermal-status.json', r.g.RUN/'thermal-samples.jsonl',
                     Path(r.__file__), Path(r.review.__file__), r.ROOT/'scripts/mixture_pressure_65_isolated.sh'):
            self.assertIn(str(path), self.plan['input_sha256'])

    def test_reprepare_same_plan_and_budget_reset_rejected(self):
        with patch.object(r, 'RUN', self.root), patch.object(r.review, 'review', return_value=copy.deepcopy(self.reviewed)):
            self.assertEqual(r.prepare()[0], self.plan)
            path = self.root/'budget.json'; original = path.read_bytes()
            path.write_text(json.dumps(dict(used_seconds=0, limit_seconds=r.BUDGET)))
            try:
                with self.assertRaisesRegex(ValueError, 'budget was reset'):
                    r.prepare()
            finally:
                path.write_bytes(original)

    def test_altered_input_rejected_without_overwrite(self):
        path = next((self.root/'inputs').glob('*/TRIM.IN')); original = path.read_bytes()
        path.write_bytes(b'changed')
        try:
            with patch.object(r, 'RUN', self.root), patch.object(r.review, 'review', return_value=copy.deepcopy(self.reviewed)):
                with self.assertRaisesRegex(ValueError, 'input bytes changed'):
                    r.prepare()
            self.assertEqual(path.read_bytes(), b'changed')
        finally:
            path.write_bytes(original)

    def test_child_binds_exact_worker_and_validated_helpers(self):
        case = next(c for c in self.plan['cases'] if c['name'] not in self.plan['retained_original_names'])
        def fake(c, contents, runtime, context, deadline):
            self.assertEqual(c, case); self.assertEqual(contents, self.inputs[case['name']])
            self.assertEqual(r.p.RUN, self.root); self.assertEqual(r.b.RUN, self.root)
            self.assertEqual(r.b.base.RUN, self.root); self.assertIs(r.p.audit_case, r.audit_case)
            self.assertIs(r.scan.launch_command, r.old.gate.launch)
            return {'status': 'COMPLETE'}
        with ExitStack() as stack:
            for module, attributes in ((r.p, ('RUN', 'audit_case', 'check_composition')),
                                       (r.b, ('RUN',)), (r.b.base, ('RUN',)), (r.scan, ('launch_command',))):
                stack.enter_context(patch.multiple(module, **{a: getattr(module, a) for a in attributes}))
            stack.enter_context(patch.object(r, 'RUN', self.root))
            stack.enter_context(patch.object(r.signal, 'signal'))
            stack.enter_context(patch.object(r.p, 'run_case', side_effect=fake))
            self.assertEqual(r.child(case['name'], self.root/'runtime', 12345), 0)
            with self.assertRaisesRegex(ValueError, 'Cannot rerun accepted'):
                r.child(self.plan['retained_original_names'][0], self.root/'runtime', 12345)

    def test_thermal_trace_changes_fail_closed(self):
        state = r.review.read('thermal-status.json')
        samples = [json.loads(x) for x in (r.g.RUN/'thermal-samples.jsonl').read_text().splitlines()]
        self.assertAlmostEqual(r.review.validate_thermal(state, samples), 78.09599709510803)
        with self.assertRaises(ValueError):
            r.review.validate_thermal(state, samples[:-1])
        changed = copy.deepcopy(samples)
        key = next(iter(changed[4]['throttle_counts'])); changed[4]['throttle_counts'][key] += 1
        with self.assertRaises(RuntimeError):
            r.review.validate_thermal(state, changed)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.event = patch.object(r.scan, 'SHUTDOWN_REQUESTED', Mock(is_set=Mock(return_value=False)))
        self.event.start(); self.addCleanup(self.event.stop)

    def test_nonzero_worker_is_never_accepted_or_retried(self):
        proc = Mock(); proc.poll.return_value = -11
        accept = Mock()
        with patch.object(r, 'launch_case', return_value=proc) as launch:
            with self.assertRaisesRegex(RuntimeError, 'exited -11'):
                r.run_batches([dict(name='a', estimated_seconds=2), dict(name='b', estimated_seconds=1)],
                              ['runtime'], r.time.monotonic()+20000, accept, Mock())
        accept.assert_not_called(); self.assertEqual(launch.call_count, 1)

    def test_completed_child_must_pass_parent_audit(self):
        proc = Mock(); proc.poll.return_value = 0
        with patch.object(r, 'launch_case', return_value=proc) as launch:
            with self.assertRaisesRegex(ValueError, 'bad audit'):
                r.run_batches([dict(name='a', estimated_seconds=2), dict(name='b', estimated_seconds=1)],
                              ['runtime'], r.time.monotonic()+20000, Mock(side_effect=ValueError('bad audit')), Mock())
        self.assertEqual(launch.call_count, 1)

    def test_job_drain_budget_prevents_dispatch(self):
        with patch.object(r, 'launch_case') as launch:
            with self.assertRaises(InterruptedError):
                r.run_batches([dict(name='a', estimated_seconds=2)], ['runtime'], r.time.monotonic()+100, Mock(), Mock())
        launch.assert_not_called()

    def test_longest_first_and_runtime_reuse_only_after_acceptance(self):
        proc = Mock(); proc.poll.return_value = 0
        accept = Mock()
        with patch.object(r, 'launch_case', return_value=proc) as launch:
            r.run_batches([dict(name='short', estimated_seconds=1), dict(name='long', estimated_seconds=2)],
                          ['runtime'], r.time.monotonic()+20000, accept, Mock())
        self.assertEqual([c.args[0]['name'] for c in launch.call_args_list], ['long', 'short'])
        self.assertEqual(accept.call_count, 2)

    def test_audit_rejects_diagnostic_and_foreign_attempt(self):
        case = dict(name='a', diagnostic=False)
        with patch.object(r.old, 'original_audit'):
            for row in (dict(case, diagnostic=True, status='COMPLETE'),
                        dict(case, status='COMPLETE', attempt='/tmp/foreign', display_startup={})):
                with self.assertRaises(ValueError):
                    r.audit_case(case, row, ())

    def test_finalizer_flags_crash_and_accounts_cleanup_time(self):
        with tempfile.TemporaryDirectory() as name, patch.object(r, 'RUN', Path(name)), \
                patch.dict(os.environ, {'SERVICE_RESULT': 'core-dump', 'EXIT_CODE': 'dumped', 'EXIT_STATUS': '11'}):
            r.write_json(r.RUN/'status.json', dict(status='RUNNING'))
            r.write_json(r.RUN/'thermal-status.json', dict(status='RUNNING', inherited_used_seconds=20398,
                         started_at=r.datetime.now().astimezone().isoformat()))
            r.write_json(r.RUN/'budget.json', dict(used_seconds=20398, limit_seconds=r.BUDGET))
            r.finalize()
            self.assertEqual(r.load('status.json')['status'], 'NEEDS_ATTENTION')
            self.assertEqual(r.load('thermal-status.json')['status'], 'NEEDS_ATTENTION')
            self.assertGreater(r.load('budget.json')['used_seconds'], 20398)

    def test_unbounded_execution_refused(self):
        with patch.object(r, 'resource_sample', return_value={'cgroup': '/tmp/not-srim'}):
            with self.assertRaisesRegex(RuntimeError, 'bounded isolated launcher'):
                r.execute(1234)

    def test_guard_stops_on_heat_and_never_resets_inherited_budget(self):
        from tests.test_crash_gate_thermal_guard import sample
        proc = Mock(pid=1234); proc.poll.return_value = None
        with tempfile.TemporaryDirectory() as name, ExitStack() as stack:
            stack.enter_context(patch.object(r, 'RUN', Path(name)))
            stack.enter_context(patch.object(r.signal, 'signal'))
            stack.enter_context(patch.object(r.thermal, 'snapshot', side_effect=[sample(), sample(85)]))
            stack.enter_context(patch.object(r.subprocess, 'Popen', return_value=proc))
            kill = stack.enter_context(patch.object(r.os, 'killpg'))
            r.write_json(r.RUN/'plan.json', dict(inherited_used_seconds=20398))
            r.write_json(r.RUN/'budget.json', dict(used_seconds=20398, limit_seconds=r.BUDGET))
            self.assertEqual(r.guard(), 1)
            self.assertEqual(r.load('thermal-status.json')['status'], 'NEEDS_ATTENTION')
            self.assertEqual(r.load('status.json')['status'], 'NEEDS_ATTENTION')
            self.assertGreater(r.load('budget.json')['used_seconds'], 20398)
            kill.assert_called_once_with(1234, signal.SIGTERM)

    def test_guard_rejects_repeat_launch_without_mutation(self):
        with tempfile.TemporaryDirectory() as name, patch.object(r, 'RUN', Path(name)):
            r.write_json(r.RUN/'status.json', dict(status='NEEDS_ATTENTION'))
            with self.assertRaisesRegex(RuntimeError, 'Already attempted'):
                r.guard()
            self.assertEqual(r.load('status.json'), dict(status='NEEDS_ATTENTION'))


if __name__ == '__main__':
    unittest.main()
