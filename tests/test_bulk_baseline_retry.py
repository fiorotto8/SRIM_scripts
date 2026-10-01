import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from scripts import bulk_baseline_retry as r


class BaselineRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (r.OLD_RUN/'status.json').exists():raise unittest.SkipTest('Preserved failed baseline unavailable')
        cls.original,cls.inputs,cls.pilot,cls.records,cls.accepted,cls.review=r.review_failed()

    def test_raw_audit_counts_and_fixed_geometry(self):
        self.assertEqual(len(self.accepted),132)
        self.assertEqual(sum(x['diagnostic'] for x in self.accepted),42)
        self.assertEqual(self.review['remaining_cases'],774)
        self.assertEqual(self.review['failed_cases'],1);self.assertEqual(self.review['stopped_cases'],14)
        checks=r.b.geometry_checks(self.accepted)
        self.assertEqual(len(checks),42);self.assertTrue(all(c['passed'] for c in checks))

    def test_measured_forecast_and_cumulative_budget(self):
        forecast,costs=r.recovery_forecast(self.original['cases'],self.accepted)
        self.assertEqual(len(costs),774)
        self.assertIn(r.FAILED_NAME,costs)
        self.assertLess(forecast['planning_hours']+self.review['inherited_used_seconds']/3600,12)
        completed={x['name'] for x in self.accepted}
        self.assertFalse(completed&set(costs))
        for case in self.original['cases']:
            if case['name'] in costs:self.assertGreaterEqual(costs[case['name']],case['estimated_seconds'])

    def test_only_known_worker_command_rewritten(self):
        runtime=Path('/private/example')
        command=['xvfb-run','-a','-e',str(runtime/'.xvfb.log'),'-s','-screen 0 1024x768x24',
                 sys.executable,str(Path(r.b.base.__file__).resolve()),'worker','--runtime',str(runtime),'--name','case']
        with patch.object(r.b.p.prior,'old_launch',return_value='ok') as launch:
            self.assertEqual(r.launch(command,runtime,100),'ok')
            rewritten=launch.call_args.args[0]
            self.assertIn(str(Path(r.worker.__file__)),rewritten)
            self.assertEqual(rewritten[-4:],command[-4:])
            with self.assertRaisesRegex(ValueError,'Unexpected'):r.launch(['wine','TRIM.exe'],runtime,100)
            self.assertEqual(launch.call_count,1)

    def test_prepare_retains_exact_cases_and_never_resets_budget(self):
        if not (r.canary.RUN/'result.json').exists():self.skipTest('Software canary not completed')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);plan,inputs,pilot=r.prepare(root)
            self.assertEqual(plan['cases'],self.original['cases'])
            self.assertEqual(inputs,self.inputs);self.assertEqual(pilot,self.pilot)
            self.assertEqual(len(plan['retained_cases']),132);self.assertEqual(plan['new_cases_to_run'],774)
            budget=json.loads((root/'budget.json').read_text());self.assertGreater(budget['used_seconds'],self.review['inherited_used_seconds'])
            budget['used_seconds']+=100;(root/'budget.json').write_text(json.dumps(budget))
            self.assertEqual(r.prepare(root)[0],plan)
            self.assertEqual(json.loads((root/'budget.json').read_text())['used_seconds'],budget['used_seconds'])
            row=json.loads((root/'cases'/plan['retained_cases'][0]/'result.json').read_text())
            self.assertIn('reused_from',row)
            (root/'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):r.prepare(root)

    def test_missing_monitor_metadata_rejects_case(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);runtime=root/'runtime';runtime.mkdir();attempt=root/'attempt';attempt.mkdir()
            row=dict(status='COMPLETE',attempt=str(attempt),artifact_sha256={})
            with patch.object(r,'RUN',root),patch.object(r.b,'run_case',return_value=row):
                result=r.run_case({'name':'case'},(b'',b''),runtime,None,0)
            self.assertEqual(result['status'],'FAILED');self.assertIn('monitor',result['error'])

    def test_scientific_energy_format_and_archived_canary(self):
        for energy in ('1000','1000.0','1000000.E-03','1.000E+03'):
            r.validate_canary_primary('Ion = F ( 9)\nEnergy = '+energy+' keV')
        for data in ('Ion = C ( 6)\nEnergy = 1000 keV','Ion = F ( 9)\nEnergy = 100 keV','missing'):
            with self.assertRaisesRegex(ValueError,'primary'):r.validate_canary_primary(data)
        if not (r.canary.RUN/'result.json').exists():self.skipTest('Software canary unavailable')
        review,_,_=r.review_software_canary()
        self.assertEqual(review['status'],'COMPLETE');self.assertEqual(review['ions_confirmed'],1)


if __name__=='__main__':unittest.main()
