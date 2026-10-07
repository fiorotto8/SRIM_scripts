import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import thermal_95_gate_retry as r


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        with patch.object(r, 'RUN', cls.root):
            cls.plan, cls.inputs = r.prepare()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_only_execution_directory_and_elapsed_budget_change(self):
        old = json.loads((r.OLD_RUN/'plan.json').read_text())
        for field in ('cases', 'thermal_policy', 'thread_environment', 'workers', 'diagnostic_histories', 'budget_seconds'):
            self.assertEqual(self.plan[field], old[field])
        self.assertEqual(self.plan['startup_recovery']['simulations_started'], 0)
        self.assertAlmostEqual(self.plan['inherited_used_seconds'], 20458.434145095107)
        self.assertAlmostEqual(self.plan['startup_recovery']['previous_failure_seconds'], .776222)

    def test_live_bindings_reproduce_plan_and_leave_frozen_module_file_unchanged(self):
        with patch.object(r, 'RUN', self.root), patch.multiple(r.gate, RUN=self.root, UNIT=r.UNIT, __file__=r.__file__, prepare=r.prepare):
            plan, inputs = r.prepare()
            self.assertEqual(plan, self.plan); self.assertEqual(inputs, self.inputs)
            self.assertEqual(r.gate.RUN, self.root)
            self.assertEqual(r.gate.__file__, r.__file__)

    def test_git_capture_works_with_explicit_working_directory(self):
        with tempfile.TemporaryDirectory() as outside:
            failed = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=outside, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        passed = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=r.ROOT, text=True).strip()
        self.assertEqual(len(passed), 40)
        launcher = (r.ROOT/'scripts/thermal_95_gate_retry.sh').read_text()
        self.assertIn('--property="WorkingDirectory=$scan_root"', launcher)
        self.assertIn('--property=RuntimeMaxSec=30min', launcher)


if __name__ == '__main__':
    unittest.main()
