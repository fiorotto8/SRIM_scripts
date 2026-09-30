import copy
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from scripts import bulk_corner_validation as c


class CornerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (c.retry.RUN / 'completion.json').exists(): raise unittest.SkipTest('Completed geometry prerequisite unavailable')
        cls.directory = tempfile.TemporaryDirectory()
        cls.plan, cls.inputs = c.prepare(Path(cls.directory.name))

    @classmethod
    def tearDownClass(cls): cls.directory.cleanup()

    def test_exact_grid_fresh_seed_pairing_and_budget(self):
        plan = self.plan
        self.assertEqual(len(plan['cases']), 37)
        self.assertEqual(sum(x['phase'] == 'canary' for x in plan['cases']), 19)
        self.assertEqual(plan['diagnostic_histories'], 1850)
        self.assertEqual(plan['independent_production_histories_added'], 0)
        points = {}
        for case in plan['cases']:
            if 'point' in case: points.setdefault(case['point'], []).append(case)
        self.assertEqual(len(points), 18)
        self.assertEqual(len({pair[0]['seed'] for pair in points.values()}), 18)
        for pair in points.values():
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0]['seed'], pair[1]['seed'])
            self.assertEqual({r['geometry'] for r in pair}, {'interior', 'interior_deep'})
        self.assertLess(plan['forecast']['planning_hours'], plan['budget_hours'])
        self.assertGreater(c.JOB_TIMEOUT / 3600, 2 * plan['forecast']['longest_reference_batch_hours'])

    def test_all_inputs_match_species_units_coordinates_and_preserve_physics(self):
        for case in self.plan['cases'][1:]:
            content, events = self.inputs[case['name']]
            self.assertTrue(events.endswith(b'\r\n\x1a'))
            lines = events[:-1].decode().splitlines()
            self.assertEqual(len(lines), 60)
            for number, line in enumerate(lines[10:], 1):
                fields = line.split()
                self.assertEqual(fields[0], f'E{number:04d}')
                self.assertEqual(int(fields[1]), case['ion_z'])
                self.assertEqual(float(fields[2]), case['energy_kev'] * 1000)
                self.assertEqual(float(fields[3]), case['origin_angstrom'])
                self.assertEqual(fields[4:], ['0', '0', '1', '0', '0'])
            original = c.d.archive_data(Path(case['source']), 'TRIM.IN').decode().splitlines()
            new = content.decode().splitlines()
            allowed = {next(i for i,x in enumerate(original) if x.startswith('1 "'))}
            allowed.update(next(i for i,x in enumerate(original) if x.startswith(header)) + 1
                           for header in ('Cascades(', 'Diskfiles', 'PlotType', 'Target material :'))
            self.assertTrue(all(a == b for i,(a,b) in enumerate(zip(original,new)) if i not in allowed))
            layer = shlex.split(next(x for x in new if x.startswith('1 "')))
            old_layer = shlex.split(next(x for x in original if x.startswith('1 "')))
            self.assertEqual(layer[1], old_layer[1])
            self.assertEqual(layer[3:], old_layer[3:])
            self.assertEqual(float(layer[2]), 2 * case['origin_angstrom'])

    def test_bad_event_inputs_rejected(self):
        for z,e,depth in [(0,1,1),(9,100,1),(9,1,0),(9,1,float('nan'))]:
            with self.assertRaises(ValueError): c.event_file(z,e,depth)

    def test_frozen_plan_and_previous_results_unchanged(self):
        self.assertEqual(c.prepare(Path(self.directory.name))[0], self.plan)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); (root/'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'): c.prepare(root)

    def test_exact_replay_and_energy_count_checks_from_raw(self):
        case = self.plan['cases'][0]; root = Path(case['replay_reference'])
        outputs = {p.name.upper():p for p in root.iterdir()}
        result = c.analyse(case, outputs)
        self.assertTrue(result['replay_passed']); self.assertEqual(result['endpoint_count'], 50)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'TDATA.TXT'; changed = dict(outputs, **{'TDATA.TXT':path})
            original = outputs['TDATA.TXT'].read_text(encoding='latin1')
            for content, error in [(original.replace('1000000.E-04', '1000000.E-03'), 'primary ion/energy'),
                                   (original.replace('000050', '000049'), 'ion count'),
                                   (original.replace('Binding = 1E-09', 'Binding = 2'), 'parameter')]:
                path.write_text(content, encoding='latin1')
                with self.assertRaisesRegex(ValueError, error): c.analyse(case, changed)

    def test_depth_gate_is_fixed_not_precision_and_requires_pair(self):
        a = dict(status='COMPLETE', point='test', geometry='interior', seed=123,
                 ionization_fraction=.5, stopped_ion_mean_dx_mm=.1)
        b = dict(a, geometry='interior_deep', ionization_fraction=.5005)
        self.assertEqual(c.depth_checks([a]), [])
        self.assertTrue(c.depth_checks([a,b])[0]['passed'])
        self.assertFalse(c.depth_checks([a,dict(b,ionization_fraction=.502)])[0]['passed'])
        self.assertFalse(c.depth_checks([a,dict(b,stopped_ion_mean_dx_mm=.101)])[0]['passed'])
        with self.assertRaisesRegex(ValueError, 'seed'): c.depth_checks([a,dict(b,seed=456)])

    def test_failed_cache_cannot_resume_and_changed_artifact_rejected(self):
        with tempfile.TemporaryDirectory() as td, patch.object(c,'RUN',Path(td)):
            case=self.plan['cases'][0]; root=Path(td)/'cases'/case['name']; root.mkdir(parents=True)
            path=root/'result.json'; path.write_text(json.dumps(dict(case,status='FAILED')))
            with self.assertRaisesRegex(RuntimeError,'Review failed'): c.cached(case)
            artifact=root/'raw'; artifact.write_text('original')
            result=dict(case,status='COMPLETE',artifact_sha256={str(artifact):c.scan.sha256_file(artifact)})
            path.write_text(json.dumps(result)); self.assertEqual(c.cached(case), result)
            artifact.write_text('changed')
            with self.assertRaisesRegex(ValueError,'artifacts changed'): c.cached(case)

    def test_review_preserves_quantity_and_no_production_claim(self):
        summary, rows, depth, surface, pressure=c.previous.review()
        self.assertEqual(summary['cases'],28);self.assertEqual(summary['production_histories_added'],0)
        self.assertEqual(summary['interior_primary_escapes'],0)
        self.assertLess(summary['max_depth_qf_relative_change'],.0001)
        self.assertEqual(len(depth),9);self.assertEqual(len(surface),9);self.assertEqual(len(pressure),6)
        self.assertIn('NOT range from the interior origin',summary['interpretation'])


if __name__ == '__main__': unittest.main()
