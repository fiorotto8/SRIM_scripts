import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch
from scripts import high_energy_pressure as p


class HighPressureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (p.previous.RUN/'completion.json').exists():raise unittest.SkipTest('Completed bulk baseline unavailable')
        cls.directory=tempfile.TemporaryDirectory();cls.root=Path(cls.directory.name)
        cls.plan,cls.inputs=p.prepare(cls.root)
    @classmethod
    def tearDownClass(cls):cls.directory.cleanup()

    def test_independent_selection_and_reserved_seed_cohort(self):
        cases=self.plan['cases'];self.assertEqual(len(cases),18);self.assertEqual(len({c['seed'] for c in cases}),18)
        self.assertEqual({c['family'] for c in cases},{'Ar','Kr','Xe'})
        self.assertEqual({c['pressure_atm'] for c in cases},{.5,3.})
        self.assertEqual({c['energy_kev'] for c in cases},{1000.})
        self.assertEqual({c['repeat'] for c in cases},{0})
        self.assertEqual(len({s for seeds in self.plan['reserved_independent_seeds'].values() for s in seeds}),72)
        for c in cases:
            self.assertEqual(c['seed'],self.plan['reserved_independent_seeds'][c['point']][0])
            self.assertEqual(c['ions'],50);self.assertEqual(c['noble_percent'],80)
        self.assertEqual(self.plan['pressure_screen_after_p2b']['comparisons'],36)
        self.assertLess(self.plan['forecast']['planning_hours'],5)

    def test_density_geometry_species_and_unchanged_physics(self):
        for case in self.plan['cases']:
            content,events=self.inputs[case['name']]
            original=p.b.base.d.archive_data(Path(case['source']),'TRIM.IN')
            control=p.b.base.d.archive_data(Path(case['pressure_source']),'TRIM.IN')
            layer=p.layer_fields(content);old=p.layer_fields(original);pressure=p.layer_fields(control)
            self.assertEqual(float(layer[3]),float(pressure[3]))
            self.assertAlmostEqual(float(layer[3])/float(old[3]),case['pressure_atm'])
            self.assertEqual(layer[4:],old[4:])
            self.assertAlmostEqual(float(layer[2])*float(layer[3]),2*float(old[2])*float(old[3]))
            self.assertEqual(float(layer[2]),2*case['origin_angstrom'])
            source=original.decode().splitlines();lines=content.decode().splitlines()
            allowed={next(i for i,x in enumerate(source) if x.startswith('1 "'))}
            allowed.update(next(i for i,x in enumerate(source) if x.startswith(h))+1
                           for h in ('Cascades(','Diskfiles','PlotType','Target material :'))
            self.assertEqual(len(lines),len(source))
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(lines,source)) if i not in allowed))
            event_lines=events[:-1].decode().splitlines()[10:];self.assertEqual(len(event_lines),50)
            for i,line in enumerate(event_lines,1):
                v=line.split();self.assertEqual(v[0],f'E{i:04d}')
                self.assertEqual(int(v[1]),case['ion_z']);self.assertEqual(float(v[2]),1e6)
                self.assertEqual(float(v[3]),case['origin_angstrom'])

    def test_invalid_density_composition_and_pressure_rejected(self):
        case=self.plan['cases'][0];original=p.b.base.d.archive_data(Path(case['source']),'TRIM.IN')
        control=p.b.base.d.archive_data(Path(case['pressure_source']),'TRIM.IN')
        with self.assertRaisesRegex(ValueError,'pressure'):p.make_input(original,control,'bad',123,case['ion_z'],1)
        with self.assertRaisesRegex(ValueError,'density'):p.make_input(original,original,'bad',123,case['ion_z'],.5)
        lines=control.decode().splitlines();index=next(i for i,x in enumerate(lines) if x.startswith('1 "'))
        fields=shlex.split(lines[index]);lines[index]=f'1 "{fields[1]}" '+' '.join(fields[2:4]+['.8']+fields[5:])
        with self.assertRaisesRegex(ValueError,'stoichiometry'):
            p.make_input(original,'\r\n'.join(lines).encode(),'bad',123,case['ion_z'],case['pressure_atm'])

    def test_actual_printed_mass_density_check(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'TDATA.TXT';case={'density_g_cm3':.0012}
            path.write_text('Layer #1 - Density = 2.00E19 atoms/cm3 = 1.200E-03 g/cm3')
            self.assertTrue(p.check_density(case,{'TDATA.TXT':path})['density_parameters_match'])
            # Actual archived Kr0.5atm header truncates to four decimal places,
            # differing by3.37% despite exact input density. Check display resolution.
            path.write_text('Density = 2E19 atoms/cm3 = .0017 g/cm3')
            self.assertTrue(p.check_density({'density_g_cm3':.0017593055246678936},{'TDATA.TXT':path})['density_parameters_match'])
            for text in ('Density = 2E19 atoms/cm3 = .0024 g/cm3','no density'):
                path.write_text(text)
                with self.assertRaisesRegex(ValueError,'density'):p.check_density(case,{'TDATA.TXT':path})

    def test_frozen_inputs_and_new_results_write_isolation(self):
        self.assertEqual(p.prepare(self.root)[0],self.plan)
        path=self.root/'plan.json';original=path.read_text()
        try:
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'Frozen'):p.prepare(self.root)
        finally:path.write_text(original)
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);runtime=root/'runtime';runtime.mkdir();attempt=root/'attempt';attempt.mkdir()
            result=dict(status='COMPLETE',attempt=str(attempt),artifact_sha256={})
            def fake(*args):
                (runtime/'.completion-probes.json').write_text('{}');return result
            with patch.object(p,'RUN',root),patch.object(p.b,'run_case',side_effect=fake),\
                 patch.object(p,'check_density',return_value={'density_parameters_match':True}):
                row=p.run_case({'name':'example'},(b'',b''),runtime,None,0)
            self.assertEqual(row['status'],'COMPLETE');self.assertTrue((root/'cases/example/result.json').exists())

    def test_baseline_summary_and_precision_limitations(self):
        summary,rows,points,timing,pressure=p.previous.review()
        self.assertEqual(len(rows),972);self.assertEqual(len(points),243);self.assertEqual(len(timing),243)
        self.assertEqual(summary['points_meeting_precision'],197);self.assertEqual(summary['precision_flags'],46)
        self.assertEqual(summary['pressure_classifications']['inconclusive'],106)
        self.assertEqual(len(pressure),108)


if __name__=='__main__':unittest.main()
