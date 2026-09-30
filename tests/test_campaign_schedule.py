import json
from collections import Counter
import unittest

from scripts import campaign_schedule as s


class ScheduleTests(unittest.TestCase):
    def test_grid_partition_and_history_accounting(self):
        plan = json.loads(s.PLAN.read_text())
        groups = s.selections(plan['target'])
        self.assertEqual(len(s.grid(plan['target'])), 3024)
        self.assertEqual(len(s.existing_grid(plan['target'])), 243)
        union = set()
        for points in groups.values():
            self.assertFalse(union & points)
            union |= points
        self.assertEqual(len(union), 2781)
        self.assertEqual(sum(stage.get('new_unique_points', 0) for stage in plan['stages']), 2781)
        batches = Counter()
        for stage in plan['stages']:
            if 'selection' in stage:
                for key in groups[stage['selection']]:
                    batches[key] += stage['batches_per_point']
        self.assertEqual(set(batches), union)
        self.assertEqual(set(batches.values()), {4})
        self.assertEqual(sum(batches.values()), 11124)
        self.assertEqual(groups['high_energy_pressure'],
                         {k for k in union if k[2] == 1000 and k[3] == 80 and k[4] in (.5, 3)})

    def test_every_dependency_precedes_stage_and_manual_controls(self):
        plan = json.loads(s.PLAN.read_text())
        seen = set()
        for stage in plan['stages']:
            self.assertLessEqual(set(stage.get('depends_on', [])), seen)
            self.assertNotIn(stage['id'], seen)
            seen.add(stage['id'])
            self.assertTrue(stage['exit_gate'])
        self.assertEqual(plan['mode'], 'manual_continue_gates_no_automatic_launches')
        self.assertEqual(plan['resources']['max_workers_and_cores'], 15)
        self.assertEqual(plan['resources']['scan_swap_max_gib'], 0)

    def test_cost_exact_and_interpolated(self):
        means = {('Kr', 'F', e, n, p): 100 * (n / 80) / p
                 for e in (1, 10, 100) for n in (60, 80, 95) for p in (.5, 1, 3)}
        self.assertEqual(s.batch_cost(('Kr', 'F', 10, 80, 1), means), 100)
        self.assertAlmostEqual(s.batch_cost(('Kr', 'F', 10, 70, 1), means), 87.5)
        self.assertAlmostEqual(s.batch_cost(('Kr', 'F', 10, 70, .5), means), 175)

    def test_scheduler_and_empty_work(self):
        means = {('Kr', 'F', 100, 80, 1): 3600.}
        result = s.estimate(set(means), 4, means, workers=2)
        self.assertEqual(result['model_hours'], 2)
        self.assertEqual(result['planning_hours'], 3.5)
        self.assertEqual(result['histories'], 200)
        with self.assertRaises(ValueError):
            s.estimate(set(), 4, means)

    def test_local_timing_reference(self):
        if not s.TIMINGS.exists():
            self.skipTest('Completed timing dataset unavailable')
        plan = json.loads(s.PLAN.read_text())
        means = s.timing_means(s.TIMINGS)
        self.assertEqual(set(means), s.existing_grid(plan['target']))
        groups = s.selections(plan['target'])
        result = s.estimate(groups['mixtures_1atm'], 4, means)
        self.assertEqual(result['batches'], 1260)
        self.assertGreater(result['planning_hours'], 10)
        self.assertLess(result['planning_hours'], 24)


if __name__ == '__main__':
    unittest.main()
