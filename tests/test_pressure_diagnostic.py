import json
from pathlib import Path
import shlex
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import pressure_diagnostic as d


INPUT = ('Ion\r\n9 18.998 100 0 50 0 0\r\nSeed\r\n2 123 0\r\n'
         'Target material : Number of Elements, Number of Layers\r\n"original" 3 1\r\n'
         'PlotType depths\r\n5 0 600\r\n1 "kr80" 600 .003 0.111 0.444 0.444\r\n'
         'Target energies\r\n1e-9 1e-9 1e-9\r\n').encode()


class PressureDiagnosticTests(unittest.TestCase):
    def test_pressure_variant_conserves_areal_density_and_seed(self):
        for p in (.5, 1, 2.2, 2.3, 3):
            lines = d.variant(INPUT, 'test', p).decode().splitlines()
            layer = shlex.split(next(x for x in lines if x.startswith('1 "')))
            self.assertAlmostEqual(float(layer[2]) * float(layer[3]), 600 * .003)
            self.assertEqual(lines[3], '2 123 0')
            self.assertEqual(lines[-1], '1e-9 1e-9 1e-9')
            self.assertEqual(lines[1], '9 18.998 100 0 50 0 0')

    def test_geometry_changes_only_selected_depths(self):
        lines = d.variant(INPUT, 'test', 3, 2, 1).decode().splitlines()
        self.assertEqual(lines[7], '5 0.0 200')
        layer = shlex.split(lines[8])
        self.assertEqual(float(layer[2]), 400)
        self.assertAlmostEqual(float(layer[3]), .009)
        self.assertIn('5 0.0 400', d.variant(INPUT, 'test', 3, 2, 2).decode())

    def test_invalid_geometry_rejected(self):
        for p, w, v in ((0, 1, 1), (1, 1, 2), (1, 1, 0)):
            with self.assertRaises(ValueError):
                d.variant(INPUT, 'test', p, w, v)

    def test_gate_requires_all_three_exact_replays(self):
        passed = dict(status='COMPLETE', replay_passed=True)
        self.assertFalse(d.replay_gate([]))
        self.assertFalse(d.replay_gate([passed] * 2))
        self.assertTrue(d.replay_gate([passed] * 3))
        self.assertFalse(d.replay_gate([passed] * 2 + [dict(status='COMPLETE', replay_passed=False)]))

    def test_comparisons_exclude_replays_and_use_matching_seed(self):
        base = dict(name='base', status='COMPLETE', phase='paired', repeat=100,
                    pressure_atm=1., width_factor=1, view_factor=1, ionization_fraction=.5,
                    pressure_times_range_mm_atm=.2, reported_to_expected_atom_density=1., backscattered_ions=1)
        rows = [base, dict(base, name='p3', pressure_atm=3., ionization_fraction=.51),
                dict(base, name='replay', phase='replay', ionization_fraction=.9)]
        result = d.comparisons(rows)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[1]['qf_relative_difference'], .02)

    def test_live_plan_and_mocked_replay_output_parsing(self):
        if not (d.prior.RUN / 'completion.json').exists():
            self.skipTest('Local completed dataset unavailable')
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            plan, inputs = d.prepare(root)
            self.assertEqual(plan['total_cases'], 36)
            self.assertEqual(plan['independent_production_histories_added'], 0)
            self.assertEqual(plan['diagnostic_histories'], 1800)
            self.assertEqual(d.prepare(root)[0], plan)
            self.assertEqual([sum(c['phase'] == p for c in plan['cases'])
                              for p in ('replay', 'paired', 'geometry')], [3, 15, 18])
            case = plan['cases'][0]
            runtime = root / 'runtime'
            (runtime / 'Data').mkdir(parents=True)
            def launch(*args, **kwargs):
                with tarfile.open(case['source']) as archive:
                    for member in archive.getmembers():
                        if member.isfile() and member.name.upper() in {x.upper() for x in d.scan.ALL_TRIM_OUTPUTS}:
                            (runtime / member.name).write_bytes(archive.extractfile(member).read())
            with patch.object(d, 'RUN', root), patch.object(d.scan, 'launch_command', side_effect=launch), \
                    patch.object(d.scan, 'pysrim_environment', return_value={}), patch.object(d.subprocess, 'run'):
                result = d.run_case(case, inputs[case['name']], runtime, None, time.monotonic() + 1000)
                self.assertEqual(result['status'], 'COMPLETE', result.get('error'))
                self.assertTrue(result['replay_passed'])
                self.assertEqual(d.cached_result(case), result)
                (Path(result['attempt']) / 'IONIZ.TXT').write_text('changed')
                with self.assertRaisesRegex(ValueError, 'artifact changed'):
                    d.cached_result(case)
            (root / 'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'):
                d.prepare(root)


if __name__ == '__main__':
    unittest.main()
