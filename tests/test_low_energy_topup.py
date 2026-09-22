from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan
from scripts import low_energy_topup as topup
from scripts.staged_scan import configured_jobs
from tests.test_precision_scan import batch


class TopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sr, cls.jobs = configured_jobs(topup.CONFIG)
        # Synthetic eight-batch, high-variance points over 64 configured keys.
        cls.keys = sorted({topup.job_key(j) for j in cls.jobs})[:64]
        cls.rows = []
        for k in cls.keys:
            for repeat in range(8):
                row = batch(k[2], repeat, 10 if repeat % 2 else 0)
                row.update(zip(('family', 'noble_percent', 'pressure_atm', 'ion', 'energy_kev'), k))
                row.update(job_id=f'old-{k}-{repeat}', random_seed=repeat + 1, wall_seconds_last_attempt=15.)
                cls.rows.append(row)

    def test_selection_is_exact_fresh_600_ions(self):
        selected = topup.select_jobs(self.rows, self.jobs)
        self.assertEqual(len(selected), 768)
        self.assertEqual({j.payload['repeat'] for j in selected}, set(range(10, 22)))
        self.assertEqual({j.payload['energy_kev'] for j in self.jobs}, {1, 3, 10})
        self.assertEqual(sum(j.payload['n_ions'] for j in selected), 38400)

    def test_seed_collision_rejected(self):
        selected = topup.select_jobs(self.rows, self.jobs)
        job = selected[0]
        rows = [dict(r) for r in self.rows]
        row = next(r for r in rows if topup.analysis.key(r) == topup.job_key(job))
        row['random_seed'] = srim_scan.seed_for(job.job_id, job.payload['repeat'])
        with self.assertRaisesRegex(ValueError, 'seed'):
            topup.select_jobs(rows, self.jobs)

    def test_changed_selection_and_incomplete_superset_rejected(self):
        with self.assertRaises(ValueError):
            topup.select_jobs(self.rows[8:], self.jobs)
        selected = topup.select_jobs(self.rows, self.jobs)
        missing = selected[0].job_id
        with self.assertRaises(ValueError):
            topup.select_jobs(self.rows, [j for j in self.jobs if j.job_id != missing])

    def test_job_ids_do_not_overlap_previous_config(self):
        _, prior = configured_jobs(topup.ROOT / 'configs/noble_cf4_precision.toml')
        self.assertFalse({j.job_id for j in prior} & {j.job_id for j in self.jobs})

    def test_empty_selection_never_runs_whole_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = topup.TopupCampaign(Path(directory))
            with patch.object(worker, 'phase') as phase:
                worker.selected_phase('empty', [], 'trim')
                phase.assert_not_called()

    def test_completed_selection_is_not_relaunched(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = topup.TopupCampaign(Path(directory))
            with patch.object(topup, 'pending', return_value=[]), patch.object(worker, 'phase') as phase:
                worker.selected_phase('complete', self.jobs[:1], 'trim')
                phase.assert_not_called()

    def test_budget_is_cumulative_across_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            topup.write_json(path / 'budget.json', {'used_seconds': 1234})
            self.assertEqual(topup.TopupCampaign(path).used, 1234)

    def test_frozen_plan_rejects_changed_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, previous, config = root / 'baseline', root / 'previous', root / 'config.toml'
            topup.write_json(config, {})
            topup.write_json(baseline / 'trim_results.csv', [])
            topup.write_json(previous / 'refinement/trim_results.csv', [])
            topup.write_json(previous / 'completion.json', {'status': 'REVIEW_REQUIRED', 'summary': {'points_present': 1890}})
            with patch.object(topup, 'BASELINE', baseline), patch.object(topup, 'PREVIOUS', previous), \
                    patch.object(topup, 'CONFIG', config), patch.object(topup, 'prior_rows', return_value=self.rows), \
                    patch.object(topup, 'configured_jobs', return_value=(self.sr, self.jobs)):
                first, _, _ = topup.prepare(root / 'run')
                self.assertEqual(topup.prepare(root / 'run')[0], first)
                topup.write_json(baseline / 'trim_results.csv', ['changed'])
                with self.assertRaisesRegex(ValueError, 'Frozen'):
                    topup.prepare(root / 'run')

    def test_budget_exhaustion_stops_child(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = topup.TopupCampaign(Path(directory))
            worker.used = topup.BUDGET
            with patch.object(topup, 'resource_sample', return_value={'memory_swap_current_gib': 0}), \
                    patch.object(topup, 'publish'), patch.object(worker, 'stop') as stop:
                worker.update()
                stop.assert_called_once()
                self.assertTrue(worker.budget_stopped)

    def test_independent_report_does_not_mix_old_batches(self):
        old = [batch(1, i) for i in range(8)]
        new = [batch(1, i) for i in range(10, 22)]
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(topup, 'prior_rows', return_value=old), patch.object(topup, 'read_new', return_value=new):
                summary = topup.publish(Path(directory), {'status': 'RUNNING'})
            self.assertEqual(summary['points_at_1000_ions'], 1)
            self.assertEqual(summary['independent_600_ion_points_complete'], 1)
            self.assertEqual(summary['completed_new_batches'], 12)
