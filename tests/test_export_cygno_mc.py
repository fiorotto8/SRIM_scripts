import importlib.util
import csv
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.integrate import quad

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("export_cygno_mc", ROOT / "scripts/export_cygno_mc.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


class ResponseTests(unittest.TestCase):
    def model(self):
        knots = np.r_[np.repeat(math.log(.1), 4), [-1., 0., 2., 4.], np.repeat(math.log(3000.), 4)]
        n = len(knots) - 4
        return {"valid_energy_keV": [.25, 1000.], "knots_log_energy": knots.tolist(),
                "coefficients": np.linspace(.15, .98, n).tolist(),
                "coefficient_covariance": (np.eye(n) * 1e-6).tolist()}

    def test_analytic_derivative_and_integral_use_total_keV_per_ion(self):
        model = self.model()
        for energy in (.25, .7, 4., 53., 999.):
            q, _, derivative, _ = export.evaluate(model, np.array([energy]))
            h = energy * 1e-5
            if energy == .25:
                plus = export.evaluate(model, np.array([energy + h]))[0][0]
                difference = ((energy + h) * plus - energy * q[0]) / h
            else:
                qq = export.evaluate(model, np.array([energy - h, energy + h]))[0]
                difference = ((energy + h) * qq[1] - (energy - h) * qq[0]) / (2 * h)
            self.assertAlmostEqual(derivative[0], difference, places=5)
        integral = quad(lambda energy: export.evaluate(model, np.array([energy]))[2][0], .25, 1000., epsabs=1e-6)[0]
        q = export.evaluate(model, np.array([.25, 1000.]))[0]
        self.assertAlmostEqual(integral, 1000 * q[1] - .25 * q[0], places=5)

    def test_boundaries_reject_subgrid_unknown_and_nonfinite_energy(self):
        export.evaluate(self.model(), np.array([.25, 1000.]))
        for energy in (0., .2499, 1000.001, float("nan"), float("inf")):
            with self.subTest(energy=energy), self.assertRaises(ValueError):
                export.evaluate(self.model(), np.array([energy]))

    def test_covariance_matches_explicit_response_variance(self):
        model = self.model()
        rng = np.random.default_rng(481)
        coefficients = rng.normal(model["coefficients"], .003, size=(500, len(model["coefficients"])))
        model["coefficient_covariance"] = np.cov(coefficients.T).tolist()
        energy = np.geomspace(.25, 1000, 50)
        _, qsem, _, dsem = export.evaluate(model, energy)
        b = export.basis(model["knots_log_energy"], np.log(energy))
        db = b + export.basis(model["knots_log_energy"], np.log(energy), 1)
        np.testing.assert_allclose(qsem, np.std(coefficients @ b.T, axis=0, ddof=1), rtol=1e-12)
        np.testing.assert_allclose(dsem, np.std(coefficients @ db.T, axis=0, ddof=1), rtol=1e-12)

    def test_constrained_fit_stays_bounded_monotone_under_noisy_input(self):
        model = self.model()
        x = np.linspace(math.log(.1), math.log(3000), 30)
        design = export.basis(model["knots_log_energy"], x)
        q = design @ model["coefficients"]
        q[12:15] -= .15
        c = export.fit_coefficients(design, q, np.repeat(.02, len(q)), .1)
        self.assertGreaterEqual(min(c), -1e-8)
        self.assertLessEqual(max(c), 1 + 1e-8)
        self.assertGreaterEqual(min(np.diff(c)), -1e-8)


class EnergyAccountingTests(unittest.TestCase):
    def records(self):
        return [{"job": {"species": "H", "energy_kevnr": 1., "n_ions": 100, "job_id": str(i)},
                 "primary_keV": p, "recoil_keV": s, "phonon_keV": .5, "binding_keV": 0., "closure_fraction": 0.}
                for i, (p, s) in enumerate(((.2, .3), (.4, .1), (.3, .2)))]

    def test_inclusive_qf_preserves_anticorrelated_channel_uncertainty(self):
        p = export.aggregate(self.records(), "fixture")[0]
        self.assertAlmostEqual(p["QF"], .5)
        self.assertAlmostEqual(p["QF_sem"], 0.)
        self.assertGreater(p["primary_electronic_keV_sem"], 0.)
        self.assertLess(p["primary_recoil_mean_covariance_keV2"], 0.)
        self.assertEqual(p["n_ions"], 300)
        variance = p["primary_electronic_keV_sem"] ** 2 + p["recoil_electronic_keV_sem"] ** 2 + 2 * p["primary_recoil_mean_covariance_keV2"]
        self.assertAlmostEqual(variance, 0.)

    def test_single_batch_sem_stays_unknown(self):
        p = export.aggregate(self.records()[:1], "fixture")[0]
        self.assertIsNone(p["QF_sem"])
        self.assertIsNone(p["primary_electronic_keV_sem"])

    def test_unequal_batch_allocation_is_rejected_instead_of_misweighted(self):
        records = self.records()
        records[0]["job"]["n_ions"] = 50
        with self.assertRaises(ValueError):
            export.aggregate(records, "fixture")

    def test_coverage_gaps_are_not_extrapolated(self):
        p = export.aggregate(self.records(), "fixture")[0]
        p["QF_sem"] = .01
        with self.assertRaisesRegex(ValueError, "bracketing"):
            export.smooth([p], .25, 1000, 30, 3)

    def test_csv_directness_is_machine_readable_and_column_contract_exact(self):
        p = export.aggregate(self.records(), "fixture")[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qf.csv"
            export.write_csv(path, [{k: p[k] for k in export.FIELDS}], export.FIELDS)
            with path.open() as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, export.FIELDS)
                self.assertEqual(next(reader)["is_direct"], "1")


if __name__ == "__main__":
    unittest.main()
