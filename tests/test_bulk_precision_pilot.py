import json
import math
from pathlib import Path
import shlex
import statistics
import tempfile
import unittest
from unittest.mock import patch
from scripts import bulk_precision_pilot as p
from scripts import bulk_campaign_schedule as schedule


class BulkPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (p.previous.RUN/'completion.json').exists():raise unittest.SkipTest('Completed bulk corners unavailable')
        cls.directory=tempfile.TemporaryDirectory();cls.root=Path(cls.directory.name)
        cls.plan,cls.inputs=p.prepare(cls.root)
    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def test_exact_independent_design(self):
        cases=self.plan['cases'];self.assertEqual(len(cases),108);self.assertEqual(len({r['seed'] for r in cases}),108)
        self.assertEqual(len({r['point'] for r in cases}),27)
        self.assertEqual(sum(r['phase']=='low_energy' for r in cases),36)
        self.assertEqual(self.plan['retained_histories'],0);self.assertEqual(self.plan['new_histories'],5400)
        self.assertLess(self.plan['forecast']['planning_hours'],4)
        for point in {r['point'] for r in cases}:
            self.assertEqual({r['repeat'] for r in cases if r['point']==point},set(range(4)))

    def test_executed_events_and_unchanged_source_physics(self):
        for case in self.plan['cases']:
            content,events=self.inputs[case['name']];lines=content.decode().splitlines()
            source=p.base.d.archive_data(Path(case['source']),'TRIM.IN').decode().splitlines()
            allowed={next(i for i,x in enumerate(source) if x.startswith('1 "'))}
            allowed.update(next(i for i,x in enumerate(source) if x.startswith(header))+1
                           for header in ('Cascades(','Diskfiles','PlotType','Target material :'))
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(lines,source)) if i not in allowed))
            layer=shlex.split(next(x for x in lines if x.startswith('1 "')))
            self.assertEqual(float(layer[2]),2*case['origin_angstrom'])
            record=events[:-1].decode().splitlines();self.assertEqual(len(record),60)
            self.assertTrue(events.endswith(b'\r\n\x1a'))
            for i,line in enumerate(record[10:],1):
                fields=line.split();self.assertEqual(fields[0],f'E{i:04d}')
                self.assertEqual(int(fields[1]),case['ion_z']);self.assertEqual(float(fields[2]),case['energy_kev']*1000)
                self.assertEqual(float(fields[3]),case['origin_angstrom'])

    def records(self):
        return [dict(self.plan['cases'][0],status='COMPLETE',repeat=i,seed=100+i,ions_confirmed=50,
                     stopped_ion_mean_dx_mm=v,ionization_fraction=v/10) for i,v in enumerate([1.,1.1,.9,1.])]

    def test_independent_batch_sem_and_no_partial_pass(self):
        rows=self.records();point=p.point_summary(rows)[0]
        self.assertEqual(point['batches'],4);self.assertEqual(point['ions'],200)
        self.assertAlmostEqual(point['projected_range_mm_sem'],statistics.stdev([1,1.1,.9,1])/2)
        self.assertFalse(point['precision_met']) # range passes; ionization relative SEM exceeds 2%.
        self.assertFalse(p.point_summary(rows[:3])[0]['precision_met'])
        self.assertIsNone(p.point_summary(rows[:1])[0]['projected_range_mm_sem'])
        stable=[dict(r,stopped_ion_mean_dx_mm=1,ionization_fraction=.5) for r in rows]
        self.assertTrue(p.point_summary(stable)[0]['precision_met'])

    def test_duplicate_seed_model_and_repeat_rejected(self):
        for change in [dict(seed=100),dict(model_id='surface'),dict(repeat=0),dict(ions_confirmed=49),dict(ionization_fraction=float('nan'))]:
            rows=self.records();rows[-1].update(change)
            with self.assertRaises(ValueError):p.point_summary(rows)

    def test_frozen_plan_and_no_old_reuse(self):
        self.assertEqual(p.prepare(self.root)[0],self.plan)
        path=self.root/'plan.json';old=path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):p.prepare(self.root)
        finally:path.write_text(old)

    def test_report_exports_and_plot_generation(self):
        rows=self.records()
        # Create the complete 27-point export shape, with distinct seeds.
        expanded=[]
        for j,case in enumerate(self.plan['cases']):
            expanded.append(dict(case,status='COMPLETE',ions_confirmed=50,ionization_fraction=.5,
                                 stopped_ion_mean_dx_mm=.1,wall_seconds=1,attempt='test',reported_range_summary_mm=2))
        with tempfile.TemporaryDirectory() as td,patch.object(p,'RUN',Path(td)):
            state={'status':'TEST'};p.publish(state,expanded);p.plots(expanded);p.publish(state,expanded)
            self.assertEqual(state['points_complete'],27);self.assertEqual(state['points_meeting_precision'],27)
            for name in ('points.csv','batches.csv','pilot.pdf','pilot.png','index.html'):self.assertTrue((Path(td)/name).is_file())

    def test_bulk_schedule_counts_no_surface_reuse(self):
        roadmap=json.loads(schedule.legacy.PLAN.read_text());_,raw,*_=p.previous.review()
        stages,totals=schedule.forecast(roadmap,raw)
        self.assertEqual(totals['surface_points_reused'],0);self.assertEqual(totals['initial_batches'],12096)
        self.assertEqual(totals['initial_histories'],604800)
        self.assertEqual(stages[0]['points'],27);self.assertEqual(stages[1]['points'],216)


if __name__=='__main__':unittest.main()
