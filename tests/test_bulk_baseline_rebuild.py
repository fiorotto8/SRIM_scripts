import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch
from scripts import bulk_baseline_rebuild as b


class BulkRebuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (b.previous.RUN/'completion.json').exists():raise unittest.SkipTest('Completed pilot unavailable')
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.plan,cls.inputs,cls.retained=b.prepare(cls.root)
    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def test_selection_counts_independence_and_diagnostic_exclusion(self):
        cases=self.plan['cases'];production=[c for c in cases if not c['diagnostic']]
        diagnostics=[c for c in cases if c['diagnostic']]
        self.assertEqual(len(cases),906);self.assertEqual(len(production),864);self.assertEqual(len(diagnostics),42)
        self.assertEqual(len({c['seed'] for c in production}),864)
        self.assertFalse({c['seed'] for c in production}&{r['seed'] for r in self.retained})
        self.assertEqual(len({b.key(c) for c in production}),216)
        self.assertEqual(len({b.key(c) for c in production+self.retained}),243)
        for d in diagnostics:
            a=next(c for c in production if c['point']==d['point'] and c['repeat']==0)
            self.assertEqual(d['seed'],a['seed']);self.assertEqual(d['width_angstrom'],2*a['width_angstrom'])
        self.assertEqual(len(b.production_rows(self.retained,cases)),972)
        self.assertEqual({phase:sum(c['phase']==phase for c in cases) for phase in ('low_geometry','high_geometry','baseline')},
                         dict(low_geometry=72,high_geometry=12,baseline=822))

    def test_all_energies_and_exact_input_preservation(self):
        seen=set()
        for case in self.plan['cases']:
            content,events=self.inputs[case['name']];source=b.base.d.archive_data(Path(case['source']),'TRIM.IN').decode().splitlines()
            lines=content.decode().splitlines();seen.add(case['energy_kev'])
            allowed={next(i for i,x in enumerate(source) if x.startswith('1 "'))}
            allowed.update(next(i for i,x in enumerate(source) if x.startswith(h))+1
                           for h in ('Cascades(','Diskfiles','PlotType','Target material :'))
            self.assertEqual(len(lines),len(source))
            self.assertTrue(all(a==c for i,(a,c) in enumerate(zip(lines,source)) if i not in allowed))
            self.assertEqual(float(shlex.split(next(x for x in lines if x.startswith('1 "')))[2]),case['width_angstrom'])
            self.assertTrue(events.endswith(b'\r\n\x1a'));records=events[:-1].decode().splitlines()[10:]
            self.assertEqual(len(records),50)
            for i,event in enumerate(records,1):
                v=event.split();self.assertEqual(v[0],f'E{i:04d}');self.assertEqual(int(v[1]),case['ion_z'])
                self.assertEqual(float(v[2]),case['energy_kev']*1000);self.assertEqual(float(v[3]),case['origin_angstrom'])
        self.assertEqual(seen,set(b.ENERGIES))

    def test_depth_gates_and_production_points(self):
        rows=[dict(c,status='COMPLETE',ions_confirmed=50,ionization_fraction=.4,stopped_ion_mean_dx_mm=.2) for c in self.plan['cases']]
        checks=b.geometry_checks(rows);self.assertEqual(len(checks),42);self.assertTrue(all(c['passed'] for c in checks))
        rows[next(i for i,r in enumerate(rows) if r['diagnostic'])]['ionization_fraction']=.5
        self.assertEqual(sum(not c['passed'] for c in b.geometry_checks(rows)),1)
        self.assertEqual(len(b.p.point_summary(b.production_rows(self.retained,rows))),243)

    def test_frozen_plan_and_budget(self):
        self.assertEqual(b.prepare(self.root)[0],self.plan)
        self.assertLessEqual(self.plan['forecast']['planning_hours'],12)
        self.assertLess(self.plan['forecast']['longest_estimated_batch_hours'],2.5)
        path=self.root/'plan.json';old=path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):b.prepare(self.root)
        finally:path.write_text(old)

    def test_pressure_family_scaling_and_followup(self):
        rows=[]
        for f in ('Ar','Kr','Xe'):
            for ion in ('C','F',f):
                for e in (1,10,100):
                    for pressure in (.5,1,3):
                        for i in range(4):
                            rows.append(dict(family=f,ion=ion,energy_kev=e,noble_percent=80,pressure_atm=pressure,
                                             status='COMPLETE',ionization_fraction=.4*(1+i*.00001),
                                             stopped_ion_mean_dx_mm=(1+i*.00001)/pressure))
        checks=b.pressure_checks(rows);self.assertEqual(len(checks),108)
        self.assertTrue(all(r['classification']=='within_working_margin' for r in checks))
        for r in rows:
            if r['pressure_atm']==3:r['ionization_fraction']*=1.2
        checks=b.pressure_checks(rows);self.assertEqual(sum(c['classification']=='followup' for c in checks),27)
        self.assertEqual(b.pressure_checks(rows[:3]),[])
        for r in rows:r['ionization_fraction']=.4;r['stopped_ion_mean_dx_mm']=1/r['pressure_atm']
        self.assertTrue(all(c['classification']=='inconclusive_zero_variance' for c in b.pressure_checks(rows)))

    def test_display_wrapper_writes_only_new_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);runtime=root/'runtime';runtime.mkdir();attempt=root/'attempt';attempt.mkdir()
            result=dict(status='COMPLETE',attempt=str(attempt),artifact_sha256={})
            def fake(*args):
                (runtime/'.display-startup.json').write_text(json.dumps(dict(allocation='Xvfb -displayfd')))
                return result
            with patch.object(b,'RUN',root),patch.object(b.p.prior,'old_run_case',side_effect=fake):
                row=b.run_case({'name':'example'},(b'',b''),runtime,None,0)
            self.assertEqual(row['status'],'COMPLETE')
            self.assertTrue((root/'cases/example/result.json').exists())
            self.assertTrue((attempt/'.display-startup.json').exists())
        self.assertEqual(b.p.RUN,b.previous.RUN)

    def test_review_rejects_changed_raw_analysis(self):
        plan,inputs=b.p.prepare(b.previous.RUN);case=plan['cases'][0]
        row=next(r for r in self.retained if r['name']==case['name'])
        b.previous.audit_case(case,row,inputs[case['name']])
        with self.assertRaisesRegex(ValueError,'Raw analysis'):
            b.previous.audit_case(case,dict(row,ionization_fraction=-1),inputs[case['name']])
        with self.assertRaisesRegex(ValueError,'Missing raw hashes'):
            b.previous.audit_case(case,dict(row,artifact_sha256={}),inputs[case['name']])


if __name__=='__main__':unittest.main()
