import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import mixture_pressure_65_capped as c


class CappedTests(unittest.TestCase):
    def test_wrong_quota_refused_before_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = Path(tmp)/c.UNIT; group.mkdir()
            (group/'cpu.max').write_text('300000 20000')
            with patch.object(c.r, 'resource_sample', return_value={'cgroup': str(group)}), patch.object(c.thermal, 'guard') as guard:
                with self.assertRaises(RuntimeError):
                    c.guard()
                guard.assert_not_called()

    def test_budget_restart_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); group = root/c.UNIT; group.mkdir()
            (group/'cpu.max').write_text(c.CPU_MAX)
            c.write_json(root/'plan.json', dict(inherited_used_seconds=100))
            c.write_json(root/'budget.json', dict(used_seconds=101, limit_seconds=c.r.BUDGET))
            with patch.object(c, 'RUN', root), patch.object(c.r, 'resource_sample', return_value={'cgroup': str(group)}):
                with self.assertRaises(ValueError):
                    c.guard()

    def test_exact_quota_and_budget_delegate_validated_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); group = root/c.UNIT; group.mkdir()
            (group/'cpu.max').write_text(c.CPU_MAX)
            c.write_json(root/'plan.json', dict(inherited_used_seconds=100))
            c.write_json(root/'budget.json', dict(used_seconds=100, limit_seconds=c.r.BUDGET))
            with patch.object(c, 'RUN', root), patch.object(c.r, 'resource_sample', return_value={'cgroup': str(group)}), patch.object(c.thermal, 'guard', return_value=0) as guard:
                self.assertEqual(c.guard(), 0)
                guard.assert_called_once_with()

    def test_production_launch_stagger(self):
        with patch.object(c.time, 'sleep') as sleep, patch.object(c, 'original_launch', return_value='child') as launch:
            self.assertEqual(c.launch_case('case', 'runtime', 123), 'child')
            sleep.assert_called_once_with(1)
            launch.assert_called_once_with('case', 'runtime', 123)

    def test_full_cold_plan_keeps_science_and_excludes_replays(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(c, 'RUN', Path(tmp)):
            plan, inputs, baseline, accepted = c.prepare()
            old = c.read(c.PRIOR, 'plan.json')
            self.assertEqual(plan['cases'], old['cases'])
            self.assertEqual(plan['pressure_screen'], old['pressure_screen'])
            self.assertEqual((len(baseline), len(accepted)), (4536, 251))
            self.assertEqual(plan['diagnostic_histories_scheduled'], 0)
            self.assertEqual(plan['inherited_used_seconds'], c.read(c.LAST, 'budget.json')['used_seconds'])
            self.assertEqual(plan['thermal_policy']['stop_at_c'], 95)
            self.assertTrue(plan['thermal_policy']['stop_on_new_throttling'])
            self.assertEqual(len(list((Path(tmp)/'inputs').iterdir())), 1009)
            for case in plan['cases']:
                if case['name'] not in plan['retained_original_names']:
                    self.assertEqual(tuple((Path(tmp)/'inputs'/case['name']/n).read_bytes() for n in ('TRIM.IN', 'TRIM.DAT')), inputs[case['name']])
            self.assertGreater(plan['forecast']['planning_hours'], 8)
            self.assertLess(plan['inherited_used_seconds']/3600+plan['forecast']['planning_hours']+3.025, 24)
            c.r.old.gate.verify_hashes(plan['input_sha256'])


if __name__ == '__main__':
    unittest.main()
