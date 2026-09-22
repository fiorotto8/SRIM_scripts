from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan
from scripts import mixture_scan as scan
from tests.test_precision_scan import batch


def rows_for(keys, repeats=range(8)):
    rows = []
    for k in sorted(keys):
        for repeat in repeats:
            row = batch(k[2], repeat)
            row.update(zip(('family','noble_percent','pressure_atm','ion','energy_kev'), k))
            row.update(job_id=f'old-{k}-{repeat}', random_seed=repeat + 1, wall_seconds_last_attempt=15.)
            rows.append(row)
    return rows


class MixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sr, cls.jobs = scan.configured_jobs(scan.CONFIG)
        cls.rows = rows_for(scan.CONTROL_KEYS)

    def test_exact_selected_grid(self):
        controls, mixture = scan.select_jobs(self.rows, self.jobs)
        self.assertEqual(len(controls), 96)
        self.assertEqual(len(mixture), 1080)
        self.assertEqual({scan.job_key(j) for j in mixture}, scan.MIXTURE_KEYS)
        self.assertEqual({scan.job_key(j) for j in controls}, scan.CONTROL_KEYS)
        self.assertTrue(all(j.payload['n_ions'] == 50 and 30 <= j.payload['repeat'] <= 37 for j in controls + mixture))
        self.assertEqual({j.payload['energy_kev'] for j in mixture}, {1,3,10,30,100})
        self.assertEqual({j.payload['condition']['pressure_atm'] for j in controls + mixture}, {1})
        self.assertEqual({j.payload['energy_max_kev'] for j in self.sr}, {1000})

    def test_missing_jobs_and_existing_new_point_rejected(self):
        controls, mixture = scan.select_jobs(self.rows, self.jobs)
        with self.assertRaises(ValueError):
            scan.select_jobs(self.rows, controls + mixture[1:])
        with self.assertRaises(ValueError):
            scan.select_jobs(self.rows + rows_for([next(iter(scan.MIXTURE_KEYS))]), self.jobs)

    def test_seed_reuse_rejected(self):
        controls, _ = scan.select_jobs(self.rows, self.jobs)
        job = controls[0]
        rows = [dict(r) for r in self.rows]
        row = next(r for r in rows if scan.analysis.key(r) == scan.job_key(job))
        row['random_seed'] = srim_scan.seed_for(job.job_id, job.payload['repeat'])
        with self.assertRaisesRegex(ValueError, 'seed'):
            scan.select_jobs(rows, self.jobs)

    def test_no_previous_job_overlap(self):
        prior = []
        for name in ('noble_cf4_production.toml','noble_cf4_precision.toml','noble_cf4_low_energy_topup.toml'):
            prior.extend(scan.configured_jobs(scan.ROOT / 'configs' / name)[1])
        self.assertFalse({j.job_id for j in self.jobs} & {j.job_id for j in prior})

    def test_forecast_interpolates_measured_neighbor_mixtures(self):
        controls, mixture = scan.select_jobs(self.rows, self.jobs)
        job = mixture[0]
        family, fraction, pressure, ion, energy = scan.job_key(job)
        rows = rows_for([(family, fraction - 5, pressure, ion, energy), (family, fraction + 5, pressure, ion, energy)])
        for r in rows:
            r['wall_seconds_last_attempt'] = 10. if r['noble_percent'] == fraction - 5 else 30.
        self.assertEqual(scan.cost_model(rows)(job), 20.)
        self.assertEqual(scan.forecast([job] * 16, scan.cost_model(rows)), 40.)

    def test_controls_are_independent_and_require_all_eight_batches(self):
        old = rows_for(scan.CONTROL_KEYS, range(4))
        for r in old:
            if r['family'] == 'Kr' and r['ion'] == 'C' and r['noble_percent'] == 80:
                r['projected_range_mm'] += 3
        new = rows_for(scan.CONTROL_KEYS, range(30,38))
        self.assertEqual(len(scan.control_contrasts(old, 'old', 4)), 8)
        self.assertEqual(len(scan.control_contrasts(new[:-1], 'new')), 6)
        contrasts = scan.control_contrasts(new, 'new')
        self.assertEqual(len(contrasts), 8)
        self.assertTrue(all(abs(r['deviation']) < 1e-12 for r in contrasts))
        self.assertTrue(any(abs(r['deviation']) > 1 for r in scan.control_contrasts(old, 'old', 4)))

    def test_new_report_preserves_independent_control_counts(self):
        new = rows_for(scan.CONTROL_KEYS | scan.MIXTURE_KEYS, range(30,38))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(scan, 'prior_rows', return_value=self.rows), patch.object(scan.topup, 'read_new', return_value=new):
                summary = scan.publish(Path(directory), {'status':'RUNNING'})
            self.assertEqual(summary['completed_new_batches'], 1176)
            self.assertEqual(summary['mixture_points_complete'], 135)
            self.assertEqual(summary['independent_controls_complete'], 12)

    def test_subclass_uses_own_budget_config_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = scan.MixtureCampaign(Path(directory))
            self.assertEqual(worker.budget_limit, 21600)
            self.assertEqual(worker.config_path, scan.CONFIG)
            with patch.object(scan, 'publish') as publish:
                worker.publish_report()
                publish.assert_called_once_with(worker.run, worker.state)
            with patch.object(worker, 'phase') as phase:
                worker.selected_phase('empty', [], 'trim')
                phase.assert_not_called()

    def test_budget_gate_rejects_overrun(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = scan.MixtureCampaign(Path(directory))
            worker.used = scan.BUDGET - 1000
            with patch.object(worker, 'update'), patch.object(scan, 'pending', return_value=self.jobs[:1]):
                with self.assertRaisesRegex(RuntimeError, 'budget'):
                    worker.gate_budget(self.jobs[:1], lambda j: 1000.)

    def test_frozen_plan_roundtrip_and_input_change(self):
        old_keys = {(family, fraction, 1., ion, float(energy))
                    for family in ('Ar','Kr','Xe') for fraction in (60,70,80,90,95)
                    for ion in (family,'C','F') for energy in scan.ENERGIES}
        rows = rows_for(old_keys)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, previous, topup_run = root/'baseline', root/'previous', root/'topup'
            config = root/'config.toml'
            for source in (config, baseline/'trim_results.csv', previous/'refinement/trim_results.csv', topup_run/'refinement/trim_results.csv'):
                scan.write_json(source, {})
            scan.write_json(topup_run/'completion.json', {'status':'TARGETS_MET','summary':{'points_present':1890}})
            with patch.object(scan.topup, 'BASELINE', baseline), patch.object(scan.topup, 'PREVIOUS', previous), \
                    patch.object(scan.topup, 'RUN', topup_run), patch.object(scan, 'CONFIG', config), \
                    patch.object(scan, 'prior_rows', return_value=rows), \
                    patch.object(scan.analysis, 'points', return_value=[None]*1890), \
                    patch.object(scan, 'configured_jobs', return_value=(self.sr, self.jobs)):
                first = scan.prepare(root/'run')[0]
                self.assertEqual(scan.prepare(root/'run')[0], first)
                self.assertEqual(len(first['srmodule_job_ids']), 39)
                scan.write_json(config, {'changed':True})
                with self.assertRaisesRegex(ValueError, 'Frozen'):
                    scan.prepare(root/'run')

    def test_control_validation_failure_blocks_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = scan.MixtureCampaign(Path(directory))
            with patch.object(scan, 'validate_selected', return_value=[['job','invalid archive']]):
                with self.assertRaisesRegex(RuntimeError, 'no subsequent phase'):
                    worker.validate_phase(self.jobs[:1], 'controls')
