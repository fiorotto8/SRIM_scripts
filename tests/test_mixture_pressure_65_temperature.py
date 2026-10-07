import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import mixture_pressure_65_temperature as g
from tests.test_thermal_95_gate import sample


class TemperatureTests(unittest.TestCase):
    def test_counter_increases_allowed_only_below_temperature_cutoff(self):
        for value in (85, 90, 94):
            g.temperature_check(sample(value, count=500), sample())
        for value in (95, 100):
            with self.assertRaises(RuntimeError):
                g.temperature_check(sample(value, count=500), sample())

    def test_reset_missing_sensor_threshold_changes_still_stop(self):
        changed = sample(); changed['critical_c']['cpu'] = 110
        missing = sample(); missing['throttle_counts'] = {}
        lost_sensor = sample(); lost_sensor['temperatures_c'] = {}
        for current in (sample(count=100), changed, missing, lost_sensor):
            with self.assertRaises(RuntimeError):
                g.temperature_check(current, sample())
        with self.assertRaises(RuntimeError):
            g.temperature_check(sample(75), sample(75), initial=True)

    def test_guard_logs_warnings_retains_trace_and_rejects_counter_reset(self):
        def fake_guard():
            baseline = sample(); baseline['time'] = 'start'
            g.thermal.thermal_check(baseline, baseline, initial=True)
            current = sample(90, count=130); current['time'] = 'later'
            g.thermal.thermal_check(current, baseline)
            self.assertEqual(current['throttle_warning']['counter_increases_since_start'], {'package':7})
            g.thermal.thermal_check(current, baseline)
            with self.assertRaises(RuntimeError):
                g.thermal.thermal_check(sample(90, count=129), baseline)
            with self.assertRaises(RuntimeError):
                g.thermal.thermal_check(sample(95, count=131), baseline)
            return 0
        original = g.thermal.thermal_check
        with tempfile.TemporaryDirectory() as tmp, patch.object(g, 'RUN', Path(tmp)), patch.object(g.c, 'guard', side_effect=fake_guard):
            self.assertEqual(g.guard(), 0)
            events = (Path(tmp)/'throttle-warnings.jsonl').read_text().splitlines()
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0])['sampled_max_c'], 90)
        self.assertIs(g.thermal.thermal_check, original)

    def test_cold_review_exact_cohort_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(g, 'RUN', Path(tmp)):
            plan, inputs, baseline, accepted = g.prepare()
            previous = g.c.read(g.PRIOR, 'plan.json')
            self.assertEqual(plan['cases'], previous['cases'])
            self.assertEqual(plan['pressure_screen'], previous['pressure_screen'])
            self.assertEqual((len(baseline), len(accepted)), (4536,251))
            self.assertEqual(plan['inherited_used_seconds'], g.c.read(g.PRIOR, 'budget.json')['used_seconds'])
            self.assertFalse(plan['thermal_policy']['stop_on_new_throttling'])
            self.assertEqual(plan['thermal_policy']['stop_at_c'],95)
            self.assertEqual(plan['diagnostic_histories_scheduled'],0)
            for case in plan['cases']:
                if case['name'] not in plan['retained_original_names']:
                    self.assertEqual(tuple((Path(tmp)/'inputs'/case['name']/n).read_bytes() for n in ('TRIM.IN','TRIM.DAT')),inputs[case['name']])
            g.r.old.gate.verify_hashes(plan['input_sha256'])


if __name__ == '__main__':
    unittest.main()
