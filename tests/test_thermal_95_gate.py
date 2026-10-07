import copy
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch
from scripts import thermal_95_gate as g


def sample(value=50, count=123):
    return dict(temperatures_c={'cpu': value}, limits_c={'cpu': 95}, critical_c={'cpu': 105},
                reported_high_c={'cpu': 85}, throttle_counts={'package': count})


class ThermalTests(unittest.TestCase):
    def test_user_amendment_allows_above85_but_not95(self):
        for value in (85, 90, 94):
            g.thermal_check(sample(value), sample())
        for value in (95, 100, 105):
            with self.assertRaises(RuntimeError):
                g.thermal_check(sample(value), sample())

    def test_preflight_still_requires_below75(self):
        with self.assertRaises(RuntimeError):
            g.thermal_check(sample(75), sample(75), initial=True)

    def test_throttling_and_changed_hardware_thresholds_hold(self):
        changed = sample(); changed['critical_c']['cpu'] = 110
        for value in (sample(count=124), changed):
            with self.assertRaises(RuntimeError):
                g.thermal_check(value, sample())

    def test_snapshot_retains_critical_headroom_and_rejects_invalid_temperature(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name)/'temp1_input'; path.with_name('temp1_crit').write_text('90000')
            raw = dict(temperatures_c={str(path): 60}, limits_c={str(path): 85}, throttle_counts={'p': 0})
            with patch.object(g.r.thermal, 'snapshot', return_value=raw):
                self.assertEqual(g.thermal_snapshot()['limits_c'][str(path)], 85)
            with patch.object(g.r.thermal, 'snapshot', return_value=dict(temperatures_c={'cpu': -1})):
                with self.assertRaises(RuntimeError):
                    g.thermal_snapshot()

    def test_emergency_stop_signals_only_members_not_guardian(self):
        group = Path('/sys/fs/cgroup/user.slice')/g.UNIT
        relative = str(group).removeprefix('/sys/fs/cgroup')
        def read(path, *args, **kwargs):
            if path == group/'cgroup.procs':
                return '100\n101\n102\n'
            return '0::'+(relative if str(path) == '/proc/101/cgroup' else '/outside')+'\n'
        with patch.object(Path, 'read_text', read), patch.object(g.os, 'getpid', return_value=100), patch.object(g.os, 'kill') as kill:
            self.assertEqual(g.kill_owned_workers(group), [101])
        self.assertEqual([call.args for call in kill.call_args_list], [(101, signal.SIGSTOP), (101, signal.SIGKILL)])

    def test_emergency_stop_refuses_broad_or_wrong_cgroup(self):
        with patch.object(g.os, 'kill') as kill:
            for path in ('/sys/fs/cgroup', '/sys/fs/cgroup/user.slice', '/tmp/other.service'):
                with self.assertRaises(RuntimeError):
                    g.kill_owned_workers(Path(path))
        kill.assert_not_called()

    def test_guard_refuses_unbounded_launch(self):
        with patch.object(g, 'resource_sample', return_value={'cgroup': '/tmp/unbounded'}):
            with self.assertRaises(RuntimeError):
                g.guard()

    def test_hot_guard_kills_owned_workers_and_preserves_trigger_and_budget(self):
        with tempfile.TemporaryDirectory() as name, ExitStack() as stack:
            root = Path(name); group = root/g.UNIT; group.mkdir()
            (group/'memory.max').write_text(str(32*g.GIB)); (group/'memory.swap.max').write_text('0')
            stack.enter_context(patch.object(g, 'RUN', root))
            stack.enter_context(patch.object(g, 'resource_sample', return_value={'cgroup':str(group)}))
            stack.enter_context(patch.object(g.os, 'sched_getaffinity', return_value=set(range(15))))
            stack.enter_context(patch.dict(os.environ, g.THREAD_ENV))
            stack.enter_context(patch.object(g.signal, 'signal'))
            stack.enter_context(patch.object(g, 'thermal_snapshot', side_effect=[sample(), sample(95)]))
            proc = Mock(); proc.poll.return_value = None
            stack.enter_context(patch.object(g.subprocess, 'Popen', return_value=proc))
            stack.enter_context(patch.object(g.subprocess, 'check_output', return_value='test-commit'))
            stop = stack.enter_context(patch.object(g, 'kill_owned_workers', return_value=[123]))
            g.write_json(root/'plan.json', dict(inherited_used_seconds=20457.657923095107))
            self.assertEqual(g.guard(), 1)
            stop.assert_called_once_with(group)
            state = g.read('thermal-status.json')
            self.assertEqual(state['status'], 'NEEDS_ATTENTION')
            self.assertEqual(state['latest']['temperatures_c']['cpu'], 95)
            self.assertEqual(state['stopped_worker_pids'], [123])
            self.assertGreater(g.read('budget.json')['used_seconds'], 20457.657923095107)
            self.assertEqual(g.read('status.json')['status'], 'NEEDS_ATTENTION')


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        with patch.object(g, 'RUN', cls.root):
            cls.plan, cls.inputs = g.prepare()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_fixed_fifteen_exact_high_energy_replays(self):
        original = json.loads((g.old.RUN/'plan.json').read_text())
        cases = {c['name']:c for c in original['cases']}
        accepted = {r['name']:r for r in json.loads((g.old.RUN/'results.json').read_text())}
        self.assertEqual(len(self.plan['cases']), 15)
        self.assertEqual(self.plan['workers'], 15)
        self.assertEqual(self.plan['diagnostic_histories'], 750)
        self.assertEqual(self.plan['new_independent_production_histories'], 0)
        for case in self.plan['cases']:
            self.assertEqual(case, dict(cases[case['name']], diagnostic=True, replay_reference=accepted[case['name']]['attempt']))
            self.assertEqual(case['energy_kev'], 1000)
            source = Path(case['replay_reference'])
            self.assertEqual(self.inputs[case['name']], ((source/'TRIM.IN').read_bytes(), (source/'TRIM.DAT').read_bytes()))
        self.assertEqual({c['family'] for c in self.plan['cases']}, {'Ar','Kr','Xe'})

    def test_inherited_budget_counts_previous_controlled_stop(self):
        self.assertAlmostEqual(self.plan['inherited_used_seconds'], 20457.657923095107)
        self.assertEqual(self.plan['budget_seconds'], 1800)
        self.assertFalse(self.plan['review']['root_cause_proven'])
        self.assertEqual(self.plan['thermal_policy']['user_ceiling_c'], 100)
        self.assertEqual(self.plan['thermal_policy']['stop_at_c'], 95)
        self.assertEqual(self.plan['thermal_policy']['poll_seconds'], .2)

    def test_freezes_all_failed_attempt_artifacts_and_previous_sources(self):
        manifest = self.plan['input_sha256']
        for path in (g.r.RUN/'cases').glob('*/result.json'):
            self.assertIn(str(path), manifest)
            for raw in json.loads(path.read_text())['artifact_sha256']:
                self.assertIn(raw, manifest)
        self.assertIn(str(g.r.RUN/'plan.json'), manifest)
        self.assertIn(str(Path(g.__file__)), manifest)


if __name__ == '__main__':
    unittest.main()
