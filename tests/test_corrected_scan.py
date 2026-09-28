import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan as scan
from scripts import corrected_scan as c
from scripts.staged_scan import configured_jobs


def reused_rows():
    cfg=scan.load_config(c.CONFIG)
    rows=[]
    for job in configured_jobs(c.SOURCE_CONFIG)[1]:
        if c.job_key(job) not in c.confirmation.KEYS:continue
        p=job.payload; condition=p['condition']
        expected=scan.requested_target_energies(('C','F',condition['family']),cfg)
        rows.append(dict(family=condition['family'],noble_percent=80,pressure_atm=1.,ion=p['ion'],energy_kev=p['energy_kev'],
                         n_ions=50,repeat=p['repeat'],job_id=job.job_id,random_seed=scan.seed_for(job.job_id,p['repeat']),
                         projected_range_mm=.01,qf_trim_ionization=.25,primary_ionization_energy_kev=.1*p['energy_kev'],
                         secondary_recoil_ionization_energy_kev=.15*p['energy_kev'],longitudinal_straggling_mm=.002,
                         raw_archive=f'raw/{job.job_id}.tar.gz',normalized_profiles=f'profiles/{job.job_id}.json.gz',
                         target_energy_check=dict(passed=True,expected=expected,observed=expected),warnings='',clipped=False))
    return rows


class CorrectedScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sr,cls.jobs=configured_jobs(c.CONFIG)

    def test_grid_exact_model_and_limits(self):
        cfg=scan.load_config(c.CONFIG)
        self.assertEqual(len(self.sr),27)
        self.assertEqual(len(self.jobs),432)
        self.assertEqual(cfg.families,('Ar','Kr','Xe'))
        self.assertEqual(cfg.noble_percent,(60,80,95))
        self.assertEqual(cfg.pressures_atm,(1.,))
        self.assertEqual({j.payload['energy_kev'] for j in self.jobs},{1,10,100,1000})
        self.assertEqual(cfg.lattice_binding_energy_ev,1e-9)
        self.assertEqual(cfg.surface_binding_energy_ev,1e-9)
        self.assertEqual(cfg.raw['physics']['model_id'],c.MODEL)
        self.assertFalse(cfg.derive_full_grid)
        self.assertEqual(cfg.campaign_max_cumulative_hours,8)
        self.assertEqual(cfg.workers_default,15)

    def test_endpoint_then_rest_disjoint_and_no_rerun_of_reuse(self):
        fresh,endpoints,rest=c.select_jobs(self.jobs,reused_rows())
        self.assertEqual((len(fresh),len(endpoints),len(rest)),(412,72,340))
        self.assertFalse({j.job_id for j in endpoints}&{j.job_id for j in rest})
        self.assertEqual({j.job_id for j in endpoints+rest},{j.job_id for j in fresh})
        self.assertFalse({c.job_key(j) for j in fresh}&c.confirmation.KEYS)
        self.assertEqual(len({c.job_key(j) for j in endpoints}),36)
        self.assertTrue(all(j.payload['repeat'] in (70,71) for j in endpoints))

    def test_wrong_reuse_count_and_ions_rejected(self):
        rows=reused_rows()
        with self.assertRaises(ValueError):c.select_jobs(self.jobs,rows[:-1])
        rows[0]['n_ions']=5
        with self.assertRaises(ValueError):c.select_jobs(self.jobs,rows)

    def test_duplicate_seed_rejected(self):
        rows=reused_rows();rows[0]['random_seed']=rows[1]['random_seed']
        with self.assertRaisesRegex(ValueError,'seeds'):c.select_jobs(self.jobs,rows)

    def test_initial_report_counts_only_accepted_cohort(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch.object(c,'completed_rows',side_effect=[(reused_rows(),[]),([],[])]):
                result=c.publish(root,dict(status='TEST'))
            self.assertEqual(result['points_complete'],5)
            self.assertEqual(result['total_histories'],1250)
            self.assertEqual(result['new_batches'],0)
            self.assertEqual(result['reused_batches'],25)
            self.assertTrue((root/'report/points.csv').exists())
            self.assertIn(c.MODEL,(root/'report/index.html').read_text())

    def test_other_epsilon_never_enters_report(self):
        rows=reused_rows()
        rows[0]['target_energy_check']['expected']={}
        with patch.object(c,'completed_rows',return_value=(rows,[])):
            with self.assertRaisesRegex(ValueError,'physics'):c.combined_rows(Path('/unused'))

    def test_no_duplicate_job_import(self):
        rows=reused_rows()
        with patch.object(c,'completed_rows',side_effect=[(rows,[]),(rows,[])]):
            with self.assertRaisesRegex(ValueError,'Duplicate'):c.combined_rows(Path('/unused'))

    def test_empty_selection_does_not_release_full_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            runner=c.CorrectedCampaign(Path(directory))
            self.assertEqual(runner.budget_limit,8*3600)
            with patch.object(runner,'phase') as phase:
                with self.assertRaises(ValueError):runner.selected('empty',c.CONFIG,Path(directory),'trim',[])
                phase.assert_not_called()

    def test_live_plan_freezes_without_mutating_previous_run(self):
        if not (c.confirmation.RUN/'completion.json').exists():self.skipTest('Local confirmation artifacts unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            plan,*_=c.prepare(root)
            self.assertEqual(plan['total_histories'],21850)
            self.assertEqual(plan['new_histories'],20600)
            self.assertLess(plan['forecast']['planning_hours'],8)
            self.assertEqual(c.prepare(root)[0],plan)
            (root/'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):c.prepare(root)


if __name__=='__main__':unittest.main()
