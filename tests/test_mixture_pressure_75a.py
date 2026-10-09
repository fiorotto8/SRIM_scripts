import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch
from scripts import mixture_pressure_75a as g


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviewed=g.previous.review()
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        with patch.object(g,'RUN',cls.root),patch.object(g.previous,'review',return_value=cls.reviewed):
            cls.plan,cls.inputs,cls.retained=g.prepare(cls.root)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_predecessor_audit_precision_and_counts(self):
        summary=self.reviewed[0]
        self.assertEqual(summary['complete_points'],1764)
        self.assertEqual(summary['precision_flags'],256)
        self.assertEqual(summary['points_meeting_precision'],1508)
        self.assertEqual(summary['peak_temperature_c'],102)
        self.assertEqual(sum(summary['pressure_classes'].values()),630)
        self.assertNotIn('followup',summary['pressure_classes'])

    def test_exact_production_grid_and_independence(self):
        cases=self.plan['cases']
        self.assertEqual(len(cases),756)
        self.assertEqual(len({g.b.key(c) for c in cases}),189)
        self.assertEqual({c['pressure_atm'] for c in cases},{.5,1.5,2})
        self.assertEqual({c['noble_percent'] for c in cases},{75})
        self.assertEqual({c['family'] for c in cases},{'Ar','Kr','Xe'})
        self.assertEqual({c['ions'] for c in cases},{50})
        self.assertFalse(any(c['diagnostic'] for c in cases))
        self.assertEqual([sum(c['phase']==p for c in cases) for p in g.PHASES],[27,27,702])
        self.assertEqual(len({x['seed'] for x in self.retained+cases}),7812)
        forbidden,_=g.seed_registry(self.root,self.retained)
        self.assertFalse({c['seed'] for c in cases}&forbidden)

    def test_ten_workers_no_cutoff_and_new_stage_budget(self):
        self.assertEqual(self.plan['workers'],10)
        self.assertIsNone(self.plan['thermal_policy']['software_temperature_cutoff_c'])
        self.assertEqual(self.plan['inherited_used_seconds'],0)
        self.assertGreater(self.plan['prior_wave_cumulative_seconds'],11175)
        self.assertLess(self.plan['forecast']['planning_hours']+3.025,24)
        self.assertAlmostEqual(self.plan['forecast']['planning_hours'],17.565459521209355)

    def test_input_bytes_hashes_and_plan_freeze(self):
        for case in self.plan['cases']:
            self.assertEqual(tuple((self.root/'inputs'/case['name']/n).read_bytes() for n in ('TRIM.IN','TRIM.DAT')),self.inputs[case['name']])
        g.gate.verify_hashes(self.plan['input_sha256'])
        with patch.object(g,'RUN',self.root),patch.object(g.previous,'review',return_value=self.reviewed):
            self.assertEqual(g.prepare(self.root)[0],self.plan)
            path=next((self.root/'inputs').glob('*/TRIM.IN'));saved=path.read_bytes();path.write_bytes(b'changed')
            try:
                with self.assertRaisesRegex(ValueError,'Frozen worker input'):g.prepare(self.root)
            finally:path.write_bytes(saved)

    def test_partial_pressure_screen_retains_full_family_correction(self):
        # Test-only relabeling verifies the same mathematical screen; never saved as production.
        rows=[dict(x,noble_percent=75) for x in self.retained if x['noble_percent']==70 and x['pressure_atm'] in g.PRESSURES]
        actual=g.pressure_checks(rows,self.plan['pressure_screen'])
        expected=[x for x in self.reviewed[4] if x['pressure_atm'] in g.PRESSURES]
        self.assertEqual(actual,expected);self.assertEqual(len(actual),378)
        spec=dict(self.plan['pressure_screen'],comparisons=378)
        with self.assertRaises(ValueError):g.pressure_checks(rows,spec)

    def test_wrong_mixture_or_geometry_rejected(self):
        from types import SimpleNamespace
        for percent,width in ((65,1),(75,0),(75,float('nan'))):
            with self.assertRaises(ValueError):g.make_input(b'',SimpleNamespace(noble_percent=percent,pressure_atm=.5),'x',1,6,1,width)


class ControllerTests(unittest.TestCase):
    def test_failed_production_gate_never_dispatches_later_phases(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);group=root/g.UNIT;group.mkdir()
            (group/'memory.max').write_text(str(32*g.GIB));(group/'memory.swap.max').write_text('0')
            stack.enter_context(patch.object(g,'RUN',root))
            stack.enter_context(patch.object(g,'resource_sample',return_value={'cgroup':str(group)}))
            stack.enter_context(patch.object(g.os,'sched_getaffinity',return_value=set(range(10))))
            stack.enter_context(patch.object(g.signal,'signal'))
            stack.enter_context(patch.object(g,'publish'))
            stack.enter_context(patch.object(g,'memory_info',return_value={'MemAvailable':40*g.GIB}))
            stack.enter_context(patch.object(g,'prepare',return_value=({'cases':[{'name':'low','phase':'low_pressure_gate'},
                                      {'name':'high','phase':'high_pressure_gate'}]}, {}, [])))
            stack.enter_context(patch.object(g.scan,'prepare_runtimes',return_value=([root/f'w{i}' for i in range(10)],None)))
            run=stack.enter_context(patch.object(g.isolated,'run_batches',side_effect=RuntimeError('gate failed')))
            self.assertEqual(g.execute(time.monotonic()+86400),1)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0],[{'name':'low','phase':'low_pressure_gate'}])
            self.assertEqual(len(run.call_args.args[1]),10)


if __name__=='__main__':unittest.main()
