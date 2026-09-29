from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan as scan
from scripts import corrected_refinement as r


def pressure_rows():
    result=[]
    for f in ('Ar','Kr','Xe'):
        for ion in ('C','F',f):
            for energy in (1,10,100):
                for pressure in (.5,1,3):
                    for repeat in range(4):
                        result.append(dict(family=f,ion=ion,energy_kev=energy,pressure_atm=pressure,noble_percent=80,
                                           repeat=repeat,projected_range_mm=(1+repeat*.01)/pressure,qf_trim_ionization=.2+repeat*.001))
    return result


class RefinementTests(unittest.TestCase):
    def test_config_grids_and_models(self):
        for name,path in r.CONFIGS.items():
            cfg=scan.load_config(path)
            _,jobs=r.configured_jobs(path)
            self.assertEqual(cfg.lattice_binding_energy_ev,1e-9)
            self.assertEqual(cfg.surface_binding_energy_ev,1e-9)
            self.assertEqual(cfg.raw['physics']['model_id'],r.base.MODEL)
            self.assertEqual((cfg.energy_min_kev,cfg.energy_max_kev),(1,1000))
            self.assertEqual(cfg.campaign_max_cumulative_hours,6)
            self.assertEqual(cfg.workers_default,15)
            self.assertFalse(cfg.derive_full_grid)
            if name=='energy':
                self.assertEqual(len(jobs),324)
                self.assertEqual({j.payload['energy_kev'] for j in jobs},{3,30,300})
                self.assertEqual(cfg.pressures_atm,(1.,))
            if name=='pressure':
                self.assertEqual(len(jobs),216)
                self.assertEqual(cfg.noble_percent,(80,))
                self.assertEqual(cfg.pressures_atm,(.5,3.))
                self.assertEqual({j.payload['energy_kev'] for j in jobs},{1,10,100})

    def test_pressure_family_and_scaled_range(self):
        result=r.pressure_checks(pressure_rows())
        self.assertEqual(len(result),54)
        self.assertTrue(all(x['flag']=='not_flagged' for x in result))
        self.assertTrue(all(x['relative_mean_spread']<1e-12 for x in result))

    def test_pressure_ignores_topup_batches(self):
        rows=pressure_rows(); expected=r.pressure_checks(rows)
        extra=[{**x,'repeat':80+x['repeat'],'projected_range_mm':100,'qf_trim_ionization':.99} for x in rows]
        self.assertEqual(expected,r.pressure_checks(rows+extra))

    def test_no_partial_family_significance(self):
        results=r.pressure_checks([x for x in pressure_rows() if x['family']=='Ar'])
        self.assertEqual(len(results),18)
        self.assertTrue(all(x['flag']=='pending_family' and x['adjusted_pvalue'] is None for x in results))

    def test_zero_variance_is_indeterminate(self):
        rows=pressure_rows()
        for x in rows:x['qf_trim_ionization']=.2
        result=r.pressure_checks(rows)
        self.assertEqual(sum(x['flag']=='indeterminate' for x in result),27)
        self.assertTrue(all(x['flag']=='not_flagged' for x in result if x['metric']=='projected_range_mm'))

    def test_strong_pressure_shift_flagged(self):
        rows=pressure_rows()
        for x in rows:
            if x['pressure_atm']==3:x['qf_trim_ionization']+=.3
        result=r.pressure_checks(rows)
        self.assertTrue(all(x['flag']=='needs_investigation' for x in result if x['metric']=='qf_trim_ionization'))

    def test_empty_selection_and_time_limit(self):
        with tempfile.TemporaryDirectory() as d:
            campaign=r.RefinementCampaign(Path(d))
            self.assertEqual(campaign.budget_limit,6*3600)
            with patch.object(campaign,'phase') as phase:
                with self.assertRaises(ValueError):campaign.selected('empty',Path('cfg'),Path(d),'trim',[])
                phase.assert_not_called()

    def test_live_frozen_plan_and_counts(self):
        if not (r.base.RUN/'completion.json').exists():self.skipTest('Completed local coarse scan unavailable')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            plan,groups,keys=r.prepare(root)
            self.assertEqual(plan['new_batches'],840)
            self.assertEqual(plan['expected_total_histories'],63850)
            self.assertLess(plan['forecast']['planning_hours'],6)
            self.assertEqual(len(keys),25)
            self.assertEqual(r.prepare(root)[0],plan)
            summary=r.publish(root,dict(status='TEST'))
            self.assertEqual(summary['points_present'],108)
            self.assertEqual(summary['new_batches'],0)
            self.assertEqual(summary['points_complete'],83)
            self.assertEqual(summary['total_histories'],21850)
            (root/'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):r.prepare(root)


if __name__=='__main__':unittest.main()
