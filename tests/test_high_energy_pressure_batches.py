import copy
import hashlib
import json
import math
from pathlib import Path
import statistics
import tempfile
import unittest
from unittest.mock import patch

from scripts import high_energy_pressure_batches as p


class PressureBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (p.previous.RUN / 'completion.json').exists():
            raise unittest.SkipTest('Completed P2a unavailable')
        cls.directory = tempfile.TemporaryDirectory(); cls.root = Path(cls.directory.name)
        cls.plan, cls.inputs, cls.retained, cls.baseline = p.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_reserved_selection_and_independent_accounting(self):
        cases = self.plan['cases']; old = json.loads((p.previous.RUN / 'plan.json').read_text())
        self.assertEqual(len(cases), 54); self.assertEqual({c['repeat'] for c in cases}, {1, 2, 3})
        self.assertEqual(len({c['point'] for c in cases}), 18)
        self.assertEqual(len({c['seed'] for c in cases + self.retained + self.baseline}), 1044)
        self.assertEqual(self.plan['pressure_screen'], old['pressure_screen_after_p2b'])
        for c in cases:
            self.assertEqual(c['seed'], old['reserved_independent_seeds'][c['point']][c['repeat']])
            self.assertEqual(c['ions'], 50); self.assertEqual(c['energy_kev'], 1000.)
            self.assertIn(c['pressure_atm'], (.5, 3.)); self.assertEqual(c['noble_percent'], 80)
        self.assertLess(self.plan['forecast']['planning_hours'], 7)

    def test_only_seed_and_labels_change(self):
        retained = {r['name']: r for r in self.retained}
        for case in self.plan['cases']:
            source = Path(retained[case['retained_batch']]['attempt'])
            old = (source / 'TRIM.IN').read_bytes(); events = (source / 'TRIM.DAT').read_bytes()
            content, new_events = self.inputs[case['name']]
            lines, source_lines = content.decode().splitlines(), old.decode().splitlines()
            allowed = {next(i for i, x in enumerate(source_lines) if x.startswith('Cascades(')) + 1,
                       source_lines.index('Target material : Number of Elements, Number of Layers') + 1}
            self.assertEqual(len(lines), len(source_lines))
            self.assertTrue(all(a == b for i, (a, b) in enumerate(zip(lines, source_lines)) if i not in allowed))
            self.assertEqual(new_events[-1:], b'\x1a')
            e, old_e = new_events[:-1].decode().splitlines(), events[:-1].decode().splitlines()
            self.assertEqual(len(e), 60); self.assertEqual(e[10:], old_e[10:])
            self.assertTrue(all(a == b for i, (a, b) in enumerate(zip(e, old_e)) if i != 7))
            self.assertEqual(hashlib.sha256(content).hexdigest(), case['input_sha256'])
            self.assertEqual(hashlib.sha256(new_events).hexdigest(), case['trim_dat_sha256'])

    def test_invalid_seed_mode_and_event_count_rejected(self):
        content = self.inputs[self.plan['cases'][0]['name']]
        for seed in (0, 900000001):
            with self.assertRaisesRegex(ValueError, 'seed'):
                p.make_input(content, 'bad', seed)
        lines = content[0].decode().splitlines()
        index = next(i for i, x in enumerate(lines) if x.startswith('Cascades(')) + 1
        lines[index] = '2 ' + ' '.join(lines[index].split()[1:])
        with self.assertRaisesRegex(ValueError, 'mode'):
            p.make_input(('\r\n'.join(lines).encode(), content[1]), 'bad', 123)
        with self.assertRaisesRegex(ValueError, 'events'):
            p.make_input((content[0], content[1].replace(b'\x1a', b'')), 'bad', 123)

    def test_p2a_summary_is_not_precision_certification(self):
        summary = self.plan['previous_review']
        self.assertEqual(summary['complete_points'], 18); self.assertEqual(summary['histories'], 900)
        self.assertLess(summary['actual_hours'], 2); self.assertEqual(summary['recovered_monitor_races'], 0)
        self.assertIn('no independent-batch SEM', summary['decision'])
        self.assertGreater(summary['lower_pressure_cost_ratio_min'], 2)

    @staticmethod
    def synthetic(zero=False):
        rows, refs = [], []
        for f in ('Ar', 'Kr', 'Xe'):
            for ion in ('C', 'F', f):
                for pressure in (.5, 1., 3.):
                    for repeat in range(4):
                        value = 1. if zero else 1. + (repeat - 1.5) * .0001
                        row = dict(status='COMPLETE', model_id=p.MODEL, family=f, ion=ion,
                                   energy_kev=1000., noble_percent=80, pressure_atm=pressure, repeat=repeat,
                                   ions_confirmed=50, seed=len(rows) + len(refs) + 1,
                                   ionization_fraction=.5 * value, stopped_ion_mean_dx_mm=value / pressure)
                        (refs if pressure == 1 else rows).append(row)
        return rows, refs

    def test_pressure_family_critical_value_and_scaling(self):
        from scipy.stats import t
        rows, refs = self.synthetic(); checks = p.pressure_checks(rows, refs, p.prior.PRESSURE_SPEC)
        self.assertEqual(len(checks), 36); self.assertEqual({c['classification'] for c in checks}, {'within_working_margin'})
        for c in checks:
            self.assertAlmostEqual(c['ratio'], 1.)
        item = next(c for c in checks if c['metric'] == 'ionization_fraction')
        values = [math.log(.5 * (1 + (i - 1.5) * .0001)) for i in range(4)]
        expected = float(t.ppf(1 - .05 / 72, 6)) * math.sqrt(2 * statistics.variance(values) / 4)
        self.assertAlmostEqual(item['ratio_high'], math.exp(expected), places=12)

    def test_zero_variance_outside_and_inconclusive_reported(self):
        rows, refs = self.synthetic(zero=True)
        self.assertEqual({c['classification'] for c in p.pressure_checks(rows, refs, p.prior.PRESSURE_SPEC)},
                         {'inconclusive_zero_variance'})
        rows, refs = self.synthetic()
        for row in rows:
            row['ionization_fraction'] *= 1.2
        labels = {c['classification'] for c in p.pressure_checks(rows, refs, p.prior.PRESSURE_SPEC)}
        self.assertIn('followup', labels)
        rows, refs = self.synthetic()
        for row in rows:
            row['ionization_fraction'] *= [0.6, .8, 1.2, 1.4][row['repeat']]
        self.assertIn('inconclusive', {c['classification'] for c in p.pressure_checks(rows, refs, p.prior.PRESSURE_SPEC)})

    def test_pressure_screen_rejects_incomplete_duplicate_and_changed_spec(self):
        rows, refs = self.synthetic()
        with self.assertRaisesRegex(ValueError, 'four'):
            p.pressure_checks(rows[:-1], refs, p.prior.PRESSURE_SPEC)
        changed = copy.deepcopy(p.prior.PRESSURE_SPEC); changed['comparisons'] = 18
        with self.assertRaisesRegex(ValueError, 'family'):
            p.pressure_checks(rows, refs, changed)
        rows[0]['seed'] = refs[0]['seed']
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            p.pressure_checks(rows, refs, p.prior.PRESSURE_SPEC)

    def test_frozen_plan_and_new_result_write_isolation(self):
        path = self.root / 'plan.json'; original = path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'):
                p.prepare(self.root)
        finally:
            path.write_text(original)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = root / 'runtime'; runtime.mkdir(); attempt = root / 'attempt'; attempt.mkdir()
            result = dict(status='COMPLETE', attempt=str(attempt), artifact_sha256={})
            def fake(*args):
                (runtime / '.completion-probes.json').write_text('{}'); return result
            with patch.object(p, 'RUN', root), patch.object(p.b, 'run_case', side_effect=fake), \
                 patch.object(p.prior, 'check_density', return_value={'density_parameters_match': True}):
                row = p.run_case({'name': 'example'}, (b'', b''), runtime, None, 0)
            self.assertEqual(row['status'], 'COMPLETE')
            self.assertTrue((root / 'cases/example/result.json').exists())


    def test_measured_high_energy_pressure_costs_are_used(self):
        from scripts import bulk_campaign_schedule as schedule
        means = {('Kr', 'F', energy, noble, 1.): float(noble * 10)
                 for energy in (1., 10., 100., 1000.) for noble in (60, 80, 95)}
        means.update({('Kr', 'F', energy, 80, pressure): 800. / pressure
                      for energy in (1., 10., 100.) for pressure in (.5, 3.)})
        means.update({('Kr', 'F', 1000., 80, .5): 1600., ('Kr', 'F', 1000., 80, 3.): 240.})
        self.assertEqual(schedule.batch_cost(('Kr', 'F', 1000., 80, 3.), means), 240.)
        self.assertEqual(schedule.batch_cost(('Kr', 'F', 1000., 70, .5), means), 1400.)
        self.assertAlmostEqual(schedule.batch_cost(('Kr', 'F', 1000., 70, 2.), means), 455.)
        self.assertAlmostEqual(schedule.batch_cost(('Kr', 'F', 100., 70, .5), means), 1400.)

    def test_schedule_lpt_and_empty_work(self):
        from scripts import bulk_campaign_schedule as schedule
        means = {('Kr', 'F', 1000., 80, .5): 3600.}
        value = schedule.estimate(set(means), 4, means, workers=2)
        self.assertEqual(value['model_hours'], 2.)
        self.assertEqual(value['planning_hours'], 3.5)
        with self.assertRaises(ValueError):
            schedule.estimate(set(), 4, means)


if __name__ == '__main__':
    unittest.main()
