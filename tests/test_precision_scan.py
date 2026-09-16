from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan
from scripts import precision_analysis as analysis
from scripts import precision_scan as campaign
from scripts.staged_scan import configured_jobs


def batch(pressure,repeat,offset=0.):
    value=10+(.1,-.1,.2,-.2)[repeat%4]+offset
    return dict(family="Ar",noble_percent=80,pressure_atm=pressure,ion="Ar",energy_kev=10.,
                repeat=repeat,n_ions=50,job_id=f"{pressure}-{repeat}",projected_range_mm=value/pressure,
                longitudinal_straggling_mm=1./pressure,qf_trim_ionization=value/100,
                primary_ionization_energy_kev=value/20,secondary_recoil_ionization_energy_kev=value/20)


class PrecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.sr,cls.jobs=configured_jobs(campaign.CONFIG)

    def test_explicit_grid_and_initial_selection(self):
        self.assertEqual(len(self.sr),270)
        self.assertEqual(len(self.jobs),15120)
        self.assertEqual({j.payload['energy_kev'] for j in self.jobs},{1,3,10,30,100,300,1000})
        self.assertEqual(len(campaign.initial_jobs(self.jobs,True)),2160)
        self.assertEqual(len(campaign.initial_jobs(self.jobs,False)),3240)
        self.assertEqual({j.payload['repeat'] for j in campaign.initial_jobs(self.jobs,True)},{2,3})
        self.assertEqual({j.payload['repeat'] for j in campaign.initial_jobs(self.jobs,False)},{2,3,4,5})
        self.assertEqual(self.sr[0].payload['energies_kev'],[1,3,10,30,100,300,1000])

    def test_adaptation_is_whole_group_with_cap(self):
        selected=campaign.adaptive_jobs(self.jobs,{('Ar',80,'Ar',10.),('Xe',95,'Xe',300.)})
        self.assertEqual(len(selected),48)
        for energy,repeats in ((10,{4,5,6,7}),(300,{6,7,8,9})):
            subset=[j for j in selected if j.payload['energy_kev']==energy]
            self.assertEqual({j.payload['repeat'] for j in subset},repeats)
            self.assertEqual(len({j.payload['condition']['pressure_atm'] for j in subset}),6)

    def test_no_reuse_of_baseline_batches(self):
        _,baseline=configured_jobs(campaign.ROOT/'configs/noble_cf4_production.toml')
        self.assertFalse({j.job_id for j in self.jobs}&{j.job_id for j in baseline})
        self.assertTrue(all(j.payload['repeat']>=2 for j in self.jobs))

    def test_invalid_explicit_grid_and_offsets_rejected(self):
        cfg=srim_scan.load_config(campaign.CONFIG)
        for energies in ([1]*7,[1000,300,100,30,10,3,1],[0,3,10,30,100,300,1000]):
            raw={**cfg.raw,'trim':{**cfg.raw['trim'],'energies_kev':energies}}
            with self.assertRaises(ValueError):srim_scan.validate_config(replace(cfg,raw=raw))
        raw={**cfg.raw,'trim':{**cfg.raw['trim'],'batch_start':-1}}
        with self.assertRaises(ValueError):srim_scan.validate_config(replace(cfg,raw=raw))

    def test_precision_requires_four_batches(self):
        self.assertFalse(analysis.points([batch(1,0),batch(1,1)])[0]['precision_met'])
        point=analysis.points([batch(1,i) for i in range(4)])[0]
        self.assertTrue(point['precision_met'])
        self.assertGreater(point['projected_range_mm_ci95_descriptive_half_width'],point['projected_range_mm_batch_sem'])

    def test_pressure_density_normalization_and_independent_confirmation(self):
        pressures=(.5,1,1.5,2,2.5,3)
        rows=[batch(p,i,5 if p==3 and i<4 else 0) for p in pressures for i in range(8)]
        discovery=analysis.pressure_tests(rows)
        confirmation=analysis.pressure_tests(rows,True)
        self.assertTrue(all(r['flag']=='pressure_difference_needs_investigation' for r in discovery))
        self.assertTrue(all(r['flag']=='not_flagged' for r in confirmation))
        self.assertTrue(all(r['relative_pressure_spread']<1e-12 for r in confirmation))
        self.assertIn(('Ar',80,'Ar',10.),analysis.scientific_selection(rows))

    def test_empty_selection_never_launches_superset(self):
        with tempfile.TemporaryDirectory() as directory:
            worker=campaign.PrecisionCampaign(Path(directory))
            with patch.object(worker,'phase') as phase:
                worker.selected_phase('empty',campaign.CONFIG,Path(directory),[])
                phase.assert_not_called()

    def test_budget_survives_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory);(path/'budget.json').write_text('{"used_seconds":1234}')
            self.assertEqual(campaign.PrecisionCampaign(path).used,1234)
