import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uproot

import srim_scan
from scripts import staged_scan, staged_report


def batch(repeat,value):
    return dict(family="Ar", noble_percent=60, pressure_atm=1., ion="Ar", energy_kev=10.,
                repeat=repeat, n_ions=50, job_id=f"job-{repeat}", random_seed=repeat+100,
                projected_range_mm=value, longitudinal_straggling_mm=1., qf_trim_ionization=value/10,
                primary_ionization_energy_kev=value/2, secondary_recoil_ionization_energy_kev=value/2,
                raw_archive="raw/test.tar.gz",wall_seconds_last_attempt=10.,
                trim_cpu_seconds_observed=8.,trim_peak_rss_mib_observed=100.)


class StagedScanTests(unittest.TestCase):
    def test_exact_production_grid_and_independent_batches(self):
        sr,jobs=staged_scan.configured_jobs(staged_scan.PRODUCTION_CONFIG)
        self.assertEqual(len(sr),270)
        self.assertEqual(len(jobs),2160)
        groups={}
        for job in jobs:
            p=job.payload
            groups.setdefault((p["condition"]["condition_id"],p["ion"],p["energy_kev"]),[]).append(job)
        self.assertEqual(len(groups),1080)
        for group in groups.values():
            self.assertEqual({j.payload["repeat"] for j in group},{0,1})
            self.assertEqual(sum(j.payload["n_ions"] for j in group),100)
            self.assertEqual(len({srim_scan.seed_for(j.job_id,j.payload["repeat"]) for j in group}),2)
        selected=staged_scan.commissioning_jobs(jobs)
        self.assertEqual(len(selected),12)
        self.assertEqual({j.payload["condition"]["family"] for j in selected},{"Ar","Kr","Xe"})
        self.assertTrue(all(j.payload["repeat"]==0 for j in selected))

    def test_kr_grid_is_small(self):
        sr,jobs=staged_scan.configured_jobs(staged_scan.KR_CONFIG)
        self.assertEqual(len(sr),6)
        self.assertEqual(len(jobs),24)
        self.assertEqual(sum(j.payload["n_ions"] for j in jobs),120)

    def test_batch_combination_mean_sem_and_variance(self):
        points=staged_report.combine_batches([batch(0,2.),batch(1,4.)])
        r=points[0]
        self.assertEqual(r["n_ions_total"],100)
        self.assertEqual(r["projected_range_mm"],3.)
        self.assertAlmostEqual(r["projected_range_mm_batch_sem"],1.)
        self.assertAlmostEqual(r["longitudinal_straggling_mm"],math.sqrt(2))
        self.assertTrue(r["all_batches_complete"])
        self.assertIsNone(staged_report.combine_batches([batch(0,2.)])[0]["projected_range_mm_batch_sem"])
        with self.assertRaises(ValueError):staged_report.combine_batches([batch(0,2.),batch(0,4.)])

    def test_root_roundtrip_and_partial_uncertainty(self):
        rows=[batch(0,2.)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"scan.root"
            staged_report.write_root(path,rows,staged_report.combine_batches(rows))
            with uproot.open(path) as root:
                self.assertEqual(root["points"].num_entries,1)
                self.assertTrue(math.isnan(root["points"]["projected_range_mm_batch_sem"].array(library="np")[0]))
                self.assertEqual(root["batches"]["job_id"].array(library="np")[0],"job-0")

    def test_missing_batch_fails_gate(self):
        _,jobs=staged_scan.configured_jobs(staged_scan.KR_CONFIG)
        with tempfile.TemporaryDirectory() as directory:
            issues=staged_scan.validate_selected(Path(directory),jobs[:1])
            self.assertEqual(len(issues),1)

    def test_complete_empty_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            staged_report.publish(root,dict(status="RUNNING",phase="test",updated_at="2026-09-15T12:00:00+02:00"))
            self.assertIn("0/1,080",(root/"report/index.html").read_text())

    def test_stop_prevents_next_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            campaign=staged_scan.Campaign(Path(directory));campaign.stopping=True
            with patch('scripts.staged_scan.subprocess.Popen') as launch:
                with self.assertRaisesRegex(RuntimeError,"operator requested stop"):
                    campaign.phase("test",staged_scan.KR_CONFIG,Path(directory),"trim")
                launch.assert_not_called()
