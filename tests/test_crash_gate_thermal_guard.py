import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import ExitStack
from scripts import crash_gate_thermal_guard as g


def sample(temp=41, count=10):
    return dict(temperatures_c={'cpu': temp}, limits_c={'cpu': 85}, throttle_counts={'package': count})


class ThermalTests(unittest.TestCase):
    def test_normal_and_warm_allowed(self):
        g.check(sample(), sample(), initial=True)
        g.check(sample(84), sample())

    def test_hot_preflight_rejected(self):
        with self.assertRaises(RuntimeError):
            g.check(sample(75), sample(75), initial=True)

    def test_high_temperature_rejected(self):
        with self.assertRaises(RuntimeError):
            g.check(sample(85), sample())

    def test_throttling_and_counter_reset_rejected(self):
        for count in (9, 11):
            with self.assertRaises(RuntimeError):
                g.check(sample(count=count), sample())

    def test_missing_or_changed_telemetry_rejected(self):
        value = copy.deepcopy(sample()); value['temperatures_c'] = {}
        with self.assertRaises(RuntimeError):
            g.check(value, sample())
        with tempfile.TemporaryDirectory() as name, self.assertRaises(RuntimeError):
            g.snapshot(Path(name), Path(name))

    def test_lower_hardware_limit_preserved(self):
        value = sample(80); value['limits_c']['cpu'] = 80
        with self.assertRaises(RuntimeError):
            g.check(value, value)

    def exercise_run(self, samples, exitcode):
        with tempfile.TemporaryDirectory() as name, ExitStack() as stack:
            root = Path(name)
            stack.enter_context(patch.object(g, 'RUN', root))
            stack.enter_context(patch.object(g, 'prepare', return_value={}))
            stack.enter_context(patch.object(g, 'snapshot', side_effect=samples))
            stack.enter_context(patch.object(g.signal, 'signal'))
            kill = stack.enter_context(patch.object(g.os, 'killpg'))
            proc = Mock(pid=1234); proc.poll.return_value = exitcode
            launch = stack.enter_context(patch.object(g.subprocess, 'Popen', return_value=proc))
            code = g.run()
            state = json.loads((root/'thermal-status.json').read_text())
            return code, state, launch.call_count, kill.call_count

    def test_success_records_thermal_gate(self):
        code, state, launched, killed = self.exercise_run([sample(), sample(50)], 0)
        self.assertEqual((code, state['status'], launched, killed), (0, 'COMPLETE', 1, 0))
        self.assertEqual(state['peak_temperature_c'], 50)

    def test_hot_preflight_never_launches(self):
        code, state, launched, killed = self.exercise_run([sample(80)], None)
        self.assertEqual((code, state['status'], launched, killed), (1, 'NEEDS_ATTENTION', 0, 0))

    def test_throttle_increase_stops_child_group(self):
        code, state, launched, killed = self.exercise_run([sample(), sample(count=11)], None)
        self.assertEqual((code, state['status'], launched, killed), (1, 'NEEDS_ATTENTION', 1, 1))

    def test_controller_failure_is_not_success(self):
        code, state, launched, killed = self.exercise_run([sample(), sample()], -11)
        self.assertEqual((code, state['status'], launched, killed), (1, 'NEEDS_ATTENTION', 1, 0))


if __name__ == '__main__':
    unittest.main()
