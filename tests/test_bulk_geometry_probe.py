from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts import bulk_geometry_probe as b


class BulkProbeTests(unittest.TestCase):
    def test_event_file_exact_units_headers_and_eof(self):
        data = b.trim_dat(12345)
        self.assertTrue(data.endswith(b'\r\n\x1a'))
        lines = data[:-1].decode('ascii').split('\r\n')
        self.assertEqual(len(lines), 61)
        self.assertEqual(lines[10], 'E0001 9 100000 12345 0 0 1 0 0')
        self.assertEqual(lines[59], 'E0050 9 100000 12345 0 0 1 0 0')
        self.assertEqual(len({line.split()[0] for line in lines[10:60]}), 50)
        for depth in (0, -1, float('nan')):
            with self.assertRaises(ValueError): b.trim_dat(depth)

    def test_endpoint_offsets_and_escape_accounting(self):
        data = 'Ion Depth Lateral Lateral\n00001 110 1 0\n00003 120 2 0\n'
        parsed = b.endpoints(data, 3, 1, 300, 100)
        self.assertEqual(parsed['endpoint_count'], 2)
        self.assertAlmostEqual(parsed['stopped_ion_mean_dx_mm'], 15e-7)
        with self.assertRaises(ValueError): b.endpoints(data, 3, 0, 300, 100)
        with self.assertRaises(ValueError): b.endpoints(data.replace('00003', '00001'), 3, 1, 300, 100)
        with self.assertRaises(ValueError): b.endpoints(data, 3, 1, 115, 100)

    def test_interior_inputs_and_unchanged_seed(self):
        # Use the exact archived input rather than imitating all SRIM headers.
        if not (b.d.prior.RUN / 'completion.json').exists(): self.skipTest('Local data unavailable')
        row = next(r for r in b.d.prior.fresh_rows(b.d.prior.RUN)
                   if (r['family'], r['ion'], r['energy_kev'], r['pressure_atm'], r['repeat']) == ('Kr', 'F', 100, 1, 100))
        original = b.d.archive_data(Path(row['raw_archive']), 'TRIM.IN')
        for geometry, factor in [('near_surface', 1), ('interior', 2), ('interior_deep', 4)]:
            content, dat, origin, width = b.make_input(original, 'test', 3, geometry)
            lines = content.decode().splitlines()
            ci = next(i for i, x in enumerate(lines) if x.startswith('Cascades')) + 1
            self.assertEqual(lines[ci].split(), ['5', str(row['random_seed']), '0'])
            self.assertAlmostEqual(width, row['target_width_angstrom'] / 3 * factor)
            self.assertEqual(origin, 1 if geometry == 'near_surface' else width / 2)
            self.assertIsNotNone(dat)
        content, dat, origin, width = b.make_input(original, 'test', 1, 'mode2_surface')
        self.assertIsNone(dat); self.assertEqual(origin, 0)

    def test_fixed_plan_and_archived_mode2_analyser(self):
        if not (b.d.RUN / 'completion.json').exists(): self.skipTest('Completed D0 unavailable')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); plan, inputs = b.prepare(root)
            self.assertEqual(plan['total_cases'], 28)
            self.assertEqual(sum(c['phase'] == 'canary' for c in plan['cases']), 2)
            self.assertEqual(sum(c['geometry'].startswith('interior') for c in plan['cases']), 18)
            self.assertEqual(plan['independent_production_histories_added'], 0)
            self.assertEqual(b.prepare(root)[0], plan)
            case = plan['cases'][0]
            outputs = {}
            with tarfile.open(case['source']) as archive:
                for member in archive.getmembers():
                    if member.isfile() and member.name.upper() in {n.upper() for n in b.scan.ALL_TRIM_OUTPUTS}:
                        path = root / member.name; path.write_bytes(archive.extractfile(member).read()); outputs[member.name.upper()] = path
            back = b.scan.count_escape_records(outputs['BACKSCAT.TXT'], 'B')
            endpath = root / 'RANGE_3D.TXT'
            endpath.write_text('\n'.join(f'{i:05d} 2500000 0 0' for i in range(1, 51 - back)))
            outputs['RANGE_3D.TXT'] = endpath
            result = b.analyse(case, outputs)
            self.assertAlmostEqual(result['ionization_fraction'], case['reference_qf'])
            self.assertEqual(result['endpoint_count'], 50 - back)
            (root / 'plan.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen'): b.prepare(root)


if __name__ == '__main__': unittest.main()
