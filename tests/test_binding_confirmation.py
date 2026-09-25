from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import binding_confirmation as study
from scripts.timing_pilot import completed_rows


class ConfirmationTests(unittest.TestCase):
    def test_grid_and_frozen_plan(self):
        # Uses the reviewed local pilot when available; pure tests below run on clones too.
        if not (study.pilot.RUN / "completion.json").exists():
            self.skipTest("Reviewed local pilot unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            plan, groups=study.prepare(root)
            self.assertEqual(plan["fresh_jobs"],50)
            self.assertEqual(plan["fresh_histories"],2500)
            self.assertEqual(plan["replay_jobs"],10)
            self.assertLess(plan["planning_hours"],4)
            for group in groups.values():
                self.assertEqual(len(group["jobs"]),25)
                self.assertEqual(len(group["sr"]),5)
                self.assertEqual(len(group["replay"]),5)
            self.assertEqual(plan,study.prepare(root)[0])
            (root/"plan.json").write_text("{}")
            with self.assertRaisesRegex(ValueError,"Frozen"):
                study.prepare(root)

    def test_interval_zero_and_large_shift(self):
        self.assertTrue(study.paired_interval([0]*5,.002)["passed"])
        self.assertFalse(study.paired_interval([.003]*5,.002)["passed"])
        self.assertFalse(study.paired_interval([-.003]*5,.002)["passed"])
        self.assertFalse(study.paired_interval([.002]*5,.002)["passed"])

    def test_uncertainty_can_fail_small_mean(self):
        result=study.paired_interval([-.002,-.001,0,.001,.002],.002)
        self.assertEqual(result["mean_relative_difference"],0)
        self.assertFalse(result["passed"])
        self.assertGreater(result["upper"],.002)

    def test_incomplete_or_nonfinite_differences_rejected(self):
        for values in ([0]*4,[0]*6,[float('nan')]*5,[float('inf')]*5):
            with self.assertRaises(ValueError):study.paired_interval(values,.002)

    def test_replay_difference_is_not_accepted(self):
        row=dict(job_id='test',raw_archive='raw.tar.gz')
        with patch.object(study,'archive_data',return_value=b'same'):
            self.assertTrue(study.compare_replay(Path('/a'),Path('/b'),row,row)['passed'])
        with patch.object(study,'archive_data',side_effect=lambda path,name: bytes(str(path),'utf8')):
            self.assertFalse(study.compare_replay(Path('/a'),Path('/b'),row,row)['passed'])

    def test_empty_selection_never_releases_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            runner=study.ConfirmationCampaign(Path(directory))
            self.assertEqual(runner.budget_limit,4*3600)
            with patch.object(runner,'phase') as phase:
                with self.assertRaises(ValueError):runner.selected('empty',Path('cfg'),Path(directory),'trim',[])
                phase.assert_not_called()

    def test_batch_independence_and_pairing(self):
        key=sorted(study.KEYS)[0]
        rows=[dict(family=key[0],noble_percent=80,pressure_atm=1.,ion=key[3],energy_kev=key[4],repeat=i,
                   random_seed=100+i,n_ions=50,target_width_angstrom=1000.,projected_range_mm=.1,
                   qf_trim_ionization=.5) for i in range(60,65)]
        with patch.object(study,'completed_rows',return_value=(rows,[])):
            result=study.analyze(Path('/unused'))
            self.assertEqual(len(result),2)
            self.assertTrue(all(r['passed'] for r in result))
        repeated=[{**r,'random_seed':100} for r in rows]
        with patch.object(study,'completed_rows',return_value=(repeated,[])):
            with self.assertRaisesRegex(ValueError,'independence'):study.analyze(Path('/unused'))
        changed=[{**r,'random_seed':r['random_seed']+1} for r in rows]
        with patch.object(study,'completed_rows',side_effect=[(rows,[]),(changed,[])]):
            with self.assertRaisesRegex(ValueError,'pairing'):study.analyze(Path('/unused'))

    def test_no_partial_precision_pass(self):
        with patch.object(study,'completed_rows',return_value=([],[])):
            self.assertEqual(study.analyze(Path('/unused')),[])


if __name__=='__main__':unittest.main()
