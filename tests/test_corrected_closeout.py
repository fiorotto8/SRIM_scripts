from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan as scan
from scripts import corrected_closeout as c


def synthetic_rows(groups):
    rows=[]
    for f,ion,e in groups:
        for p in (.5,1.,3.):
            for i in range(12):
                rows.append(dict(family=f,ion=ion,energy_kev=e,noble_percent=80,pressure_atm=p,
                                 projected_range_mm=(1+.01*i)/p,qf_trim_ionization=.2+.001*i))
    return rows


class CloseoutTests(unittest.TestCase):
    def test_config_model_and_bounds(self):
        cfg=scan.load_config(c.CONFIG)
        self.assertEqual(cfg.lattice_binding_energy_ev,1e-9)
        self.assertEqual(cfg.surface_binding_energy_ev,1e-9)
        self.assertEqual(cfg.raw['physics']['model_id'],c.MODEL)
        self.assertEqual((cfg.energy_min_kev,cfg.energy_max_kev),(1.,1000.))
        self.assertEqual(cfg.trim_ions,50)
        self.assertEqual(cfg.raw['trim']['batches_per_point'],12)
        self.assertEqual(cfg.raw['trim']['batch_start'],100)
        self.assertEqual(cfg.campaign_max_cumulative_hours,3)
        self.assertEqual(cfg.workers_default,15)

    def test_independent_scaled_ranges_and_counts(self):
        groups={('Ar','Ar',1.),('Kr','F',1.)}
        results=c.independent_pressure(synthetic_rows(groups),groups)
        self.assertEqual(len(results),4)
        self.assertTrue(all(x['flag']=='not_flagged' and x['independent_batches_per_pressure']==12 for x in results))
        self.assertTrue(all(x['relative_mean_spread']<1e-12 for x in results))

    def test_incomplete_family_not_tested(self):
        groups={('Ar','Ar',1.),('Kr','F',1.)}
        rows=synthetic_rows({('Ar','Ar',1.)})
        result=c.independent_pressure(rows,groups)
        self.assertEqual(len(result),2)
        self.assertTrue(all(x['flag']=='pending_family' and x['adjusted_pvalue'] is None for x in result))

    def test_too_few_histories_not_tested(self):
        groups={('Ar','Ar',1.)}
        self.assertEqual(c.independent_pressure(synthetic_rows(groups)[:-1],groups),[])

    def test_zero_variance_is_not_equivalence(self):
        groups={('Ar','Ar',1.)};rows=synthetic_rows(groups)
        for x in rows:x['qf_trim_ionization']=.2
        result=c.independent_pressure(rows,groups)
        q=next(x for x in result if x['metric']=='qf_trim_ionization')
        self.assertEqual(q['flag'],'indeterminate')
        self.assertIsNone(q['adjusted_pvalue'])

    def test_large_pressure_shift_flagged(self):
        groups={('Ar','Ar',1.)};rows=synthetic_rows(groups)
        for x in rows:
            if x['pressure_atm']==3:x['qf_trim_ionization']+=.3
        result=c.independent_pressure(rows,groups)
        self.assertEqual(next(x for x in result if x['metric']=='qf_trim_ionization')['flag'],'needs_investigation')

    def test_empty_selection_never_releases_full_grid(self):
        with tempfile.TemporaryDirectory() as d:
            campaign=c.CloseoutCampaign(Path(d));self.assertEqual(campaign.budget_limit,3*3600)
            with patch.object(campaign,'phase') as phase:
                with self.assertRaises(ValueError):campaign.selected('empty',c.CONFIG,Path(d),'trim',[])
                phase.assert_not_called()

    def test_live_selection_frozen_and_report(self):
        if not (c.previous.RUN/'completion.json').exists():self.skipTest('Local reviewed dataset unavailable')
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);plan,sr,jobs=c.prepare(root)
            self.assertEqual(len(jobs),564)
            self.assertEqual(len(plan['selected_keys']),47)
            self.assertEqual(len(plan['precision_keys']),35)
            self.assertEqual(len(plan['pressure_groups']),6)
            self.assertEqual(plan['expected_total_histories'],92050)
            self.assertEqual(sum(r['ions'] for r in plan['expected_history_counts']),92050)
            self.assertLess(plan['planning_minutes'],180)
            self.assertEqual(c.prepare(root)[0],plan)
            report=c.publish(root,dict(status='TEST'))
            self.assertEqual(report['total_histories'],63850)
            self.assertEqual(report['points_present'],243)
            self.assertEqual(report['new_batches'],0)
            self.assertEqual(report['points_complete'],196)
            (root/'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):c.prepare(root)


if __name__=='__main__':unittest.main()
