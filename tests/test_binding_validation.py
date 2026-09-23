from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import srim_scan as scan
from scripts import binding_validation as pilot


def tdata(value="1e-9", symbols=("C", "F", "Kr")):
    return "\r\n".join(f"Target energies for target atom = {s}\r\n"
                      f"Displacement = 21 eV, Binding = {value} eV, Surface = {value} eV" for s in symbols)


class BindingValidationTests(unittest.TestCase):
    def setUp(self):
        self.expected = {s: dict(displacement=21., lattice=1e-9, surface=1e-9) for s in ("C", "F", "Kr")}

    def test_small_positive_values_confirmed(self):
        self.assertTrue(scan.check_target_energies(tdata(), self.expected)["passed"])

    def test_no_absolute_tolerance_can_hide_zero(self):
        self.assertFalse(scan.check_target_energies(tdata("0"), self.expected)["passed"])
        zeros = {s: dict(displacement=21., lattice=0., surface=0.) for s in self.expected}
        self.assertFalse(scan.check_target_energies(tdata("2"), zeros)["passed"])

    def test_relative_rounding_and_malformed_data(self):
        self.assertTrue(scan.check_target_energies(tdata("1.000001e-9"), self.expected)["passed"])
        for text in ("", tdata(symbols=("C", "F")), tdata() + "\n" + tdata(),
                     tdata("nan"), tdata("inf"), tdata("garbled"), tdata("-1e-9"),
                     tdata() + "\nTarget energies for target atom = Xe malformed"):
            with self.subTest(text=text):
                self.assertFalse(scan.check_target_energies(text, self.expected)["passed"])

    def test_displacement_also_checked(self):
        self.assertFalse(scan.check_target_energies(tdata().replace("21 eV", "22 eV"), self.expected)["passed"])

    def test_archive_check_does_not_trust_recorded_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "TDATA.TXT"
            data.write_text(tdata("0"))
            archive = root / "raw.tar.gz"
            scan.archive_files_atomic(archive, {"TDATA.TXT": data}, {"target_energy_check": {"passed": True}})
            self.assertFalse(scan.check_archived_target_energies(archive, self.expected)["passed"])
            self.assertFalse(scan.check_archived_target_energies(root / "missing", self.expected)["passed"])

    def test_mismatch_stops_queue_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = scan.open_database(root)
            jobs = [scan.JobSpec(f"job-{i}", "trim", {"energy_kev": 1.}) for i in range(3)]
            called = []

            def worker(job, runtime):
                called.append(job.job_id)
                raise scan.TargetParameterError("TARGET_ENERGY_MISMATCH")

            with self.assertRaisesRegex(RuntimeError, "TARGET_ENERGY_MISMATCH"):
                scan.execute_stage(jobs, "trim", connection, root, [root / "worker"], worker, True, 3, None)
            self.assertEqual(called, [jobs[0].job_id])
            self.assertEqual(scan.job_status(connection, jobs[0].job_id)[0], "failed")
            self.assertEqual(scan.job_status(connection, jobs[1].job_id)[0], "pending")
            connection.close()

    def test_grid_pairing_and_frozen_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, grids = pilot.prepare(root)
            self.assertEqual(plan["total_jobs"], 72)
            self.assertEqual(plan["total_simulated_histories"], 360)
            for model, (stopping, jobs) in grids.items():
                cfg = scan.load_config(pilot.CONFIGS[model])
                self.assertEqual(len(stopping), 9)
                self.assertEqual({j.payload["condition"]["family"] for j in jobs}, {"Ar", "Kr", "Xe"})
                self.assertEqual({j.payload["energy_kev"] for j in jobs}, {1, 10, 100, 1000})
                self.assertGreater(cfg.lattice_binding_energy_ev, 0)
                self.assertEqual(cfg.energy_max_kev, 1000)
                self.assertFalse(cfg.derive_full_grid)
            self.assertEqual(pilot.prepare(root)[0], plan)
            (root / "plan.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Frozen"):
                pilot.prepare(root)

    def test_empty_selection_never_runs_full_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = pilot.ValidationCampaign(Path(directory))
            with patch.object(runner, "phase") as phase:
                with self.assertRaises(ValueError):
                    runner.selected("empty", Path("config"), Path(directory), "trim", [])
                phase.assert_not_called()
            self.assertEqual(runner.budget_limit, 7200)

    def test_pair_requires_identical_seed_geometry_and_output(self):
        row = dict(job_id="x", family="Kr", ion="C", energy_kev=1., random_seed=123, n_ions=5,
                   noble_percent=80, pressure_atm=1., target_width_angstrom=1000., target_width_attempts=1,
                   projected_range_mm=.1, qf_trim_ionization=.5, target_energy_check={"passed": True})
        outputs = {"RANGE.TXT": "range-hash", "IONIZ.TXT": "ioniz-hash"}
        self.assertTrue(pilot.compare_pair(row, row, outputs, outputs)["passed"])
        for changed in ({"random_seed": 456}, {"target_width_angstrom": 2000}, {"target_energy_check": {"passed": False}}):
            self.assertFalse(pilot.compare_pair(row, {**row, **changed}, outputs, outputs)["passed"])
        self.assertFalse(pilot.compare_pair(row, row, outputs, {**outputs, "IONIZ.TXT": "different"})["passed"])


if __name__ == "__main__":
    unittest.main()
