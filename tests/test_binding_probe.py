import unittest
from scripts import binding_probe as probe


class BindingProbeTests(unittest.TestCase):
    def setUp(self):
        self.original=('unchanged seed and geometry\r\n'+''.join(h+'\r\n 21.0 21.0 21.0\r\n' for h in probe.HEADERS)+'tail\r\n').encode()

    def test_baseline_is_byte_identical(self):
        self.assertEqual(probe.input_variant(self.original,'original',False),self.original)

    def test_only_three_parameter_rows_change(self):
        old=self.original.decode().splitlines()
        for _,style,markers in probe.CASES:
            new=probe.input_variant(self.original,style,markers).decode().splitlines()
            self.assertEqual(len(old),len(new))
            self.assertTrue(all(a==b for i,(a,b) in enumerate(zip(old,new)) if i not in (2,4,6)))

    def test_padded_markers_and_line_endings(self):
        data=probe.input_variant(self.original,'padded',True)
        self.assertIn(b'    31.0    32.0    33.0\r\n',data)
        self.assertIn(b'     1.0     3.0     5.0\r\n',data)
        self.assertNotIn(b'\n',data.replace(b'\r\n',b''))

    def test_comma_markers(self):
        self.assertIn(b' 6.0,7.0,9.0\r\n',probe.input_variant(self.original,'comma',True))
        self.assertEqual(len(probe.CASES),8)
        self.assertEqual(len({c[0] for c in probe.CASES}),8)

    def test_expected_parameters_differ_by_element(self):
        self.assertEqual(probe.expected_values(True)['F'],dict(displacement=32.,lattice=3.,surface=7.))
        self.assertTrue(all(v['lattice']==v['surface']==0 for v in probe.expected_values(False).values()))
