import math
import unittest

from scripts import review_corrected_closeout as review


class ReviewTests(unittest.TestCase):
    def fixture(self):
        row = dict(job_id='test', family='Kr', ion='F', condition_id='kr80',
                   noble_percent=80, pressure_atm=3., temperature_k=293.15,
                   energy_kev=100., density_g_cm3=.01, target_width_angstrom=100.,
                   qf_trim_ionization=.003, target_energy_check=dict(expected={
                       'Kr': dict(displacement=21., lattice=1e-9, surface=1e-9)}),
                   backscattered_ions=2, transmitted_ions=0)
        inputs = '1 "kr80" 100 .01 .111 .444 .444\n0 Target layer phases\n1\n'
        tdata = ('Density = 1.351E19 atoms/cm3\nTarget energies for target atom = Kr\n'
                 'Displacement = 21 eV, Binding = 1E-09 eV, Surface = 1E-09 eV\n')
        ioniz = 'eV /(Angstrom-Ion)\n' + '\n'.join(f'{i} 1 2' for i in range(1, 101))
        return row, inputs, tdata, ioniz

    def test_expected_density_scales_with_pressure(self):
        row = self.fixture()[0]
        high = review.expected_atom_density(row)
        self.assertTrue(math.isclose(high, 1.352e20, rel_tol=.001))
        self.assertEqual(review.expected_atom_density({**row, 'pressure_atm': 1}), high / 3)

    def test_density_discrepancy_is_retained_not_silently_corrected(self):
        result = review.raw_check(*self.fixture())
        self.assertTrue(result['density_header_mismatch'])
        self.assertAlmostEqual(result['reported_to_expected'], .1, places=3)
        self.assertAlmostEqual(result['qf_reintegrated'], .003)
        self.assertEqual(result['endpoint_width_qf_relative_change'], 0)

    def test_rejects_incorrect_input_density(self):
        row, inputs, tdata, ioniz = self.fixture()
        with self.assertRaisesRegex(ValueError, 'input density'):
            review.raw_check(row, inputs.replace('.01', '.02'), tdata, ioniz)

    def test_rejects_wrong_gas_phase(self):
        row, inputs, tdata, ioniz = self.fixture()
        with self.assertRaisesRegex(ValueError, 'gas'):
            review.raw_check(row, inputs.replace('phases\n1', 'phases\n0'), tdata, ioniz)

    def test_rejects_different_integral_or_binding(self):
        row, inputs, tdata, ioniz = self.fixture()
        with self.assertRaisesRegex(ValueError, 'integral'):
            review.raw_check({**row, 'qf_trim_ionization': .01}, inputs, tdata, ioniz)
        with self.assertRaisesRegex(ValueError, 'binding'):
            review.raw_check(row, inputs, tdata.replace('Binding = 1E-09', 'Binding = 4'), ioniz)

    def test_rejects_incomplete_pressure_cohort(self):
        with self.assertRaisesRegex(ValueError, 'twelve'):
            review.pressure_details([])


if __name__ == '__main__':
    unittest.main()
