"""Scientific configuration checks; none of these tests launch SRIM/TRIM."""

from collections import defaultdict
from copy import deepcopy
import csv
import json
import math
import os
import statistics
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import cygno_study as study


ROOT = Path(__file__).resolve().parents[1]
SRIM_ROOT = Path(os.environ.get("SRIM_ROOT", ROOT)).resolve()
CONFIG = ROOT / "configs" / "cygno_hecf4_900mbar.toml"

# Neutral atomic isotope masses from the NIST table recorded in methodology.md.
# These deliberately differ from the installed natural-element target masses.
PROJECTILES = {
    "H": (1, 1.00782503223),
    "He": (2, 4.00260325413),
    "C": (6, 12.0),
    "F": (9, 18.99840316273),
    "O": (8, 15.99491461957),
}


class GasCompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = study.load_configuration(CONFIG)
        cls.elements, _ = study.stopping.load_elements(SRIM_ROOT / "Data" / "ATOMDATA")
        cls.conditions = {
            row["medium"]: row
            for row in study.gas_conditions(cls.config, cls.elements)
        }

    def assertFractionsAlmostEqual(self, actual, expected):
        self.assertEqual(set(actual), set(expected))
        for symbol, value in expected.items():
            self.assertAlmostEqual(actual[symbol], value, places=14, msg=symbol)

    def test_baseline_molecular_atomic_and_mass_fractions_are_distinct(self):
        baseline = self.conditions["baseline"]
        self.assertEqual(baseline["molecular_fractions"], {"He": 0.6, "CF4": 0.4})
        self.assertFractionsAlmostEqual(
            baseline["element_counts_per_molecule"], {"He": 0.6, "C": 0.4, "F": 1.6}
        )
        self.assertFractionsAlmostEqual(
            baseline["atomic_fractions"], {"He": 3 / 13, "C": 2 / 13, "F": 8 / 13}
        )
        he = self.elements["He"].mass_u
        carbon = self.elements["C"].mass_u
        fluorine = self.elements["F"].mass_u
        molar_mass = 0.6 * he + 0.4 * (carbon + 4 * fluorine)
        self.assertFractionsAlmostEqual(baseline["mass_fractions"], {
            "He": 0.6 * he / molar_mass,
            "C": 0.4 * carbon / molar_mass,
            "F": 1.6 * fluorine / molar_mass,
        })
        self.assertAlmostEqual(baseline["molecular_weight_g_mol"], molar_mass)
        self.assertAlmostEqual(sum(baseline["atomic_fractions"].values()), 1)
        self.assertAlmostEqual(sum(baseline["mass_fractions"].values()), 1)
        self.assertNotAlmostEqual(baseline["atomic_fractions"]["He"], 0.6)
        self.assertNotAlmostEqual(
            baseline["mass_fractions"]["He"], baseline["atomic_fractions"]["He"]
        )

    def test_both_impurities_use_final_mole_fractions_and_correct_atom_counts(self):
        wet = self.conditions["contaminated"]
        self.assertFractionsAlmostEqual(wet["molecular_fractions"], {
            "He": 0.5988, "CF4": 0.3992, "H2O": 0.001, "O2": 0.001,
        })
        self.assertAlmostEqual(sum(wet["molecular_fractions"].values()), 1)
        self.assertAlmostEqual(wet["molecular_fractions"]["He"] /
                               wet["molecular_fractions"]["CF4"], 1.5)
        counts = {"He": 0.5988, "C": 0.3992, "F": 1.5968, "H": 0.002, "O": 0.003}
        self.assertFractionsAlmostEqual(wet["element_counts_per_molecule"], counts)
        self.assertAlmostEqual(sum(counts.values()), 2.5998)
        self.assertFractionsAlmostEqual(wet["atomic_fractions"], {
            symbol: count / 2.5998 for symbol, count in counts.items()
        })
        wet_molar_mass = (
            0.5988 * self.elements["He"].mass_u
            + 0.3992 * (self.elements["C"].mass_u + 4 * self.elements["F"].mass_u)
            + 0.001 * (2 * self.elements["H"].mass_u + self.elements["O"].mass_u)
            + 0.001 * (2 * self.elements["O"].mass_u)
        )
        self.assertAlmostEqual(wet["molecular_weight_g_mol"], wet_molar_mass)
        self.assertFractionsAlmostEqual(wet["mass_fractions"], {
            symbol: count * self.elements[symbol].mass_u / wet_molar_mass
            for symbol, count in counts.items()
        })
        self.assertAlmostEqual(sum(wet["mass_fractions"].values()), 1)

    def test_ideal_gas_density_uses_installed_293k_source_and_si_conversion(self):
        header = (ROOT / "GasDensities.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn("293 K", header)
        self.assertEqual(self.config["study"]["temperature_k"], 293.0)
        self.assertEqual(self.config["study"]["pressure_mbar"], 900.0)
        for condition in self.conditions.values():
            with self.subTest(medium=condition["medium"]):
                # P/(RT) is mol/m3; multiply by kg/mol and divide kg/m3 by 1000.
                expected_kg_m3 = (
                    90_000 / (8.31446261815324 * 293)
                    * condition["molecular_weight_g_mol"] / 1000
                )
                self.assertAlmostEqual(condition["density_g_cm3"], expected_kg_m3 / 1000,
                                       places=15)
                gas = study.as_gas(condition)
                self.assertEqual(gas.stoich, condition["element_counts_per_molecule"])
                self.assertAlmostEqual(gas.density_g_cm3, expected_kg_m3 / 1000)
        self.assertTrue(0.0013 < self.conditions["baseline"]["density_g_cm3"] < 0.0015)


class ProjectileAndJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = study.load_configuration(CONFIG)

    def test_projectiles_include_h_and_o_without_target_membership_filter(self):
        self.assertEqual(set(self.config["study"]["species"]), set(PROJECTILES))
        self.assertEqual(study.ISOTOPE_MASSES,
                         {symbol: mass for symbol, (_, mass) in PROJECTILES.items()})
        with mock.patch.object(study.stopping, "select_ions",
                               side_effect=AssertionError("Do not filter projectiles by target")):
            jobs = study.make_jobs(self.config, "production", Path("unused"))
        expected_events = self.config["simulation"]["ions_per_batch"]
        per_medium = defaultdict(set)
        per_species = defaultdict(set)
        for job in jobs:
            per_medium[job["medium"]].add(job["species"])
            per_species[job["species"]].add(job["n_ions"])
        self.assertEqual(per_medium, {"baseline": set(PROJECTILES), "contaminated": set(PROJECTILES)})
        self.assertTrue(all(values == {expected_events} for values in per_species.values()))
        # Species abundance is not the Monte Carlo sampling weight.
        no_impurities = deepcopy(self.config)
        no_impurities["study"]["h2o_ppm_molar"] = 0
        no_impurities["study"]["o2_ppm_molar"] = 0
        self.assertEqual(study.make_jobs(no_impurities, "production", Path("unused")), jobs)

    def test_seeds_pair_media_but_independently_replicate_batches(self):
        jobs = study.make_jobs(self.config, "production", Path("unused"))
        self.assertEqual(jobs, study.make_jobs(self.config, "production", Path("unused")))
        self.assertEqual(len({job["job_id"] for job in jobs}), len(jobs))
        paired = defaultdict(dict)
        for job in jobs:
            paired[(job["species"], job["energy_kevnr"], job["repeat"])][job["medium"]] = job
        for key, media in paired.items():
            with self.subTest(point=key):
                self.assertEqual(set(media), {"baseline", "contaminated"})
                self.assertEqual(media["baseline"]["seed"], media["contaminated"]["seed"])
                self.assertNotEqual(media["baseline"]["job_id"], media["contaminated"]["job_id"])
        independent = [media["baseline"]["seed"] for media in paired.values()]
        self.assertEqual(len(set(independent)), len(independent))
        self.assertEqual(len(independent), len(PROJECTILES) * self.config["grid"]["energy_points"]
                         * self.config["simulation"]["independent_batches"])

    def test_written_bulk_inputs_keep_each_isotope_and_natural_target_masses(self):
        elements, _ = study.stopping.load_elements(SRIM_ROOT / "Data" / "ATOMDATA")
        config = deepcopy(self.config)
        config["grid"]["pilot_energies_kevnr"] = [10.0]
        config["grid"]["pilot_ions"] = 2
        conditions = study.gas_conditions(config, elements)
        metadata = {"gas_conditions": conditions}
        baseline = next(row for row in conditions if row["medium"] == "baseline")
        self.assertNotIn("H", baseline["element_counts_per_molecule"])
        self.assertNotIn("O", baseline["element_counts_per_molecule"])

        class NativeLaunchForbidden(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            for job in study.make_jobs(config, "pilot", output):
                if job["medium"] != "baseline":
                    continue
                with self.subTest(projectile=job["species"]):
                    runtime = root / job["species"]
                    runtime.mkdir()
                    with mock.patch.object(study.stopping, "parse_stopping_output", return_value=[]), \
                         mock.patch.object(study.stopping, "interpolate_stopping", return_value=SimpleNamespace(
                             projected_range_cm=0.01, longitudinal_straggling_cm=0.002)), \
                         mock.patch.object(study, "invoke_native_trim", side_effect=NativeLaunchForbidden), \
                         mock.patch.object(study.stopping, "select_ions", side_effect=AssertionError(
                             "Projectile must not be selected from the target")):
                        with self.assertRaises(NativeLaunchForbidden):
                            study.run_one(job, runtime, output, config, metadata, elements)
                    lines = (runtime / "TRIM.IN").read_text().splitlines()
                    ion_fields = lines[lines.index(next(line for line in lines
                                                        if line.startswith("Ion: "))) + 1].split()
                    z, mass = PROJECTILES[job["species"]]
                    self.assertEqual(int(ion_fields[0]), z)
                    self.assertAlmostEqual(float(ion_fields[1]), mass, delta=0.5e-5)
                    self.assertGreater(float(ion_fields[2]), 10)
                    self.assertLessEqual(float(ion_fields[2]), 10.001)
                    self.assertEqual(int(ion_fields[4]), 2)
                    cascade = lines[next(index for index, line in enumerate(lines)
                                         if line.startswith("Cascade Type")) + 1].split()
                    self.assertEqual(int(cascade[0]), 5)
                    self.assertEqual(int(cascade[1]), job["seed"])
                    phase_index = next(index for index, line in enumerate(lines)
                                       if "Target layer phases" in line)
                    self.assertEqual(lines[phase_index + 1].strip(), "1")
                    for line in lines:
                        if line.startswith("Atom "):
                            fields = line.split()
                            symbol = fields[3]
                            self.assertEqual(float(fields[-1]), elements[symbol].mass_u)
                    events = (runtime / "TRIM.dat").read_bytes().rstrip(b"\x1a").decode("ascii").splitlines()[10:]
                    self.assertEqual(len(events), 2)
                    for event in events:
                        fields = event.split()
                        self.assertEqual(int(fields[1]), z)
                        self.assertEqual(float(fields[2]), 10_000)
                        # The known prediction and configured safety margins yield a bulk midpoint.
                        width = config["simulation"]["target_width_factor"] * (
                            0.01 + config["simulation"]["target_sigma_margin"] * 0.002) * 1e8
                        self.assertTrue(math.isclose(float(fields[3]), width / 2))
                        self.assertEqual([float(value) for value in fields[6:]], [1.0, 0.0, 0.0])
            self.assertFalse((output / "batches").exists())


class DetachedCampaignTests(unittest.TestCase):
    def test_failed_detached_campaign_records_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(study, "campaign", return_value=False):
                self.assertFalse(study.run_campaign(output, output, {}, {}, {}, 1))
            state = json.loads((output / "campaign_state.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertIn("finished_at", state)
            self.assertFalse(study.study_status(output)["campaign_process_active"])

    def test_detached_campaign_exception_is_recorded_and_propagated(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with mock.patch.object(study, "campaign", side_effect=RuntimeError("native input rejected")):
                with self.assertRaisesRegex(RuntimeError, "native input rejected"):
                    study.run_campaign(output, output, {}, {}, {}, 1)
            state = json.loads((output / "campaign_state.json").read_text())
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["error"], "native input rejected")

    def test_status_detects_missing_process_instead_of_stale_running_record(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "campaign_state.json").write_text(json.dumps({
                "pid": os.getpid(), "host": study.socket.gethostname(), "status": "running"}))
            # This unittest process is alive but is not the detached campaign.
            status = study.study_status(output)
            self.assertFalse(status["campaign_process_active"])
            self.assertEqual(status["campaign"]["status"], "interrupted_or_process_missing")


class InterruptedAnalysisTests(unittest.TestCase):
    def test_disjoint_completed_medium_repeats_preserve_missing_paired_statistics(self):
        config = deepcopy(study.load_configuration(CONFIG))
        config["study"]["species"] = ["H"]
        config["grid"]["energy_points"] = 2
        records = [
            {"ion_id": 1, "projected_displacement_mm": 0.9,
             "lateral_y_mm": 0.1, "lateral_z_mm": 0.0, "endpoint_displacement_3d_mm": 0.91},
            {"ion_id": 2, "projected_displacement_mm": 1.1,
             "lateral_y_mm": -0.1, "lateral_z_mm": 0.0, "endpoint_displacement_3d_mm": 1.11},
        ]
        budget = {"primary_electronic_energy_kev": 4.0, "secondary_electronic_energy_kev": 1.0,
                  "phonon_energy_kev": 5.0, "exclusive_energy_budget_with_binding_residual_fraction": 0.0}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "batches").mkdir()
            (output / "provenance").mkdir()
            (output / "metadata.json").write_text("{}", encoding="utf-8")
            (output / "provenance" / "source_installation.json").write_text(
                json.dumps({"fingerprint": {"fixture": "source"}, "control": {"fixture": "controls"}}),
                encoding="utf-8")
            # An interrupted concurrent run can finish baseline repeat 0 and
            # contaminated repeat 1, without any completed matched repeat yet.
            for medium, repeat in (("baseline", 0), ("contaminated", 1)):
                batch = {
                    "job": {"stage": "production", "medium": medium, "species": "H",
                            "repeat": repeat, "energy_kevnr": 10.0, "n_ions": 2,
                            "job_id": f"interrupted-{medium}", "seed": repeat + 100},
                    "qf": 0.5, "eee_kev": 5.0, "endpoints": {"records": records, "projected_range_mm": 1.0},
                    "paths": None, "budget": budget, "clipped": False,
                    "backscattered": 0, "transmitted": 0,
                }
                (output / "batches" / f"{medium}.json").write_text(json.dumps(batch), encoding="utf-8")
            with mock.patch.object(study.core, "source_fingerprint", return_value={"fixture": "source"}), \
                 mock.patch.object(study.core, "source_control_fingerprint", return_value={"fixture": "controls"}):
                validation = study.analyse(output, config, output / "unused-source")
            with (output / "comparison.csv").open(newline="", encoding="utf-8") as handle:
                comparison = list(csv.DictReader(handle))
            self.assertEqual(len(comparison), 1)
            self.assertEqual(comparison[0]["n_pairs"], "0")
            for field in ("qf_difference", "qf_difference_sem", "qf_relative_change",
                          "range_difference_mm", "range_difference_mm_sem", "range_relative_change_sem",
                          "confidence_t_factor"):
                self.assertEqual(comparison[0][field], "", msg=field)
            self.assertEqual(comparison[0]["impurity_equivalence_resolved"], "False")
            self.assertEqual(comparison[0]["impurity_classification"], "inconclusive")
            self.assertEqual(comparison[0]["impurity_classification_required"], "True")
            diagnostics = json.loads((output / "impurity_diagnostics.json").read_text())
            self.assertEqual(diagnostics["rows"][0]["n_pairs"], 0)
            impurity_check = next(row for row in validation["checks"]
                                  if row["check"] == "impurity_qf_classification")
            self.assertFalse(impurity_check["passed"])
            self.assertFalse(validation["passed"])
            self.assertEqual(validation["status"], "incomplete")


class JointLateralUncertaintyTests(unittest.TestCase):
    def test_joint_delete_one_uncertainty_matches_explicit_recomputed_samples(self):
        y = [0.0, 0.7, 2.4, -1.1, 3.8]
        z = [0.0, -0.2, 2.8, -1.4, 3.0]
        expected_sd = math.sqrt((statistics.variance(y) + statistics.variance(z)) / 2)
        deleted = [math.sqrt((statistics.variance(y[:index] + y[index + 1:])
                              + statistics.variance(z[:index] + z[index + 1:])) / 2)
                   for index in range(len(y))]
        center = statistics.fmean(deleted)
        expected_sem = math.sqrt((len(y) - 1) / len(y)
                                 * sum((value - center) ** 2 for value in deleted))
        sd, sem = study.lateral_sd_and_jackknife(y, z)
        self.assertAlmostEqual(sd, expected_sd, places=13)
        self.assertAlmostEqual(sem, expected_sem, places=13)

    def test_combined_dispersion_and_joint_uncertainty_are_rotation_invariant(self):
        y = [-1.7, 0.3, 4.8, 1.2, -0.6]
        z = [0.4, -2.5, 3.4, 0.7, 2.1]
        angle = 0.731
        rotated_y = [math.cos(angle) * a - math.sin(angle) * b for a, b in zip(y, z)]
        rotated_z = [math.sin(angle) * a + math.cos(angle) * b for a, b in zip(y, z)]
        original = study.lateral_sd_and_jackknife(y, z)
        rotated = study.lateral_sd_and_jackknife(rotated_y, rotated_z)
        self.assertAlmostEqual(original[0], rotated[0], places=13)
        self.assertAlmostEqual(original[1], rotated[1], places=13)

    def test_small_samples_preserve_unknown_uncertainty_and_coordinate_pairing(self):
        self.assertEqual(study.lateral_sd_and_jackknife([], []), (None, None))
        self.assertEqual(study.lateral_sd_and_jackknife([1.0], [2.0]), (None, None))
        sd, sem = study.lateral_sd_and_jackknife([1.0, 3.0], [-1.0, 3.0])
        self.assertAlmostEqual(sd, math.sqrt(5))
        self.assertIsNone(sem)
        with self.assertRaisesRegex(ValueError, "paired endpoints"):
            study.lateral_sd_and_jackknife([1.0, 2.0], [3.0])


class EnergyChannelSummaryTests(unittest.TestCase):
    def analyse_channels(self, missing=False):
        config = deepcopy(study.load_configuration(CONFIG))
        config["study"]["species"] = ["H"]
        config["grid"]["energy_points"] = 2
        records = [{"ion_id": index, "projected_displacement_mm": value,
                    "lateral_y_mm": 0.0, "lateral_z_mm": 0.0,
                    "endpoint_displacement_3d_mm": value}
                   for index, value in enumerate((0.9, 1.1), 1)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "batches").mkdir()
            (output / "provenance").mkdir()
            (output / "metadata.json").write_text("{}", encoding="utf-8")
            (output / "provenance" / "source_installation.json").write_text(
                json.dumps({"fingerprint": {}, "control": {}}), encoding="utf-8")
            for repeat in range(3):
                binding = 0.1 * (repeat + 1)
                pre_binding_residual = 0.5 * (repeat + 1)
                residual = pre_binding_residual - binding
                budget = {"primary_electronic_energy_kev": 3.0 + repeat,
                          "secondary_electronic_energy_kev": 2.0 - repeat,
                          "phonon_energy_kev": 4.5 - 0.5 * repeat,
                          "remaining_energy_kev": 5.0,
                          "binding_energy_sink_kev": binding,
                          "exclusive_energy_budget_residual_kev": pre_binding_residual,
                          "exclusive_energy_budget_with_binding_residual_kev": residual,
                          "exclusive_energy_budget_with_binding_residual_fraction": residual / 10,
                          "recoil_transfer_diagnostic_columns_kev": [3.0 + 0.3 * repeat, 1.0 + 0.2 * repeat]}
                if missing:
                    for field in ("binding_energy_sink_kev", "exclusive_energy_budget_residual_kev",
                                  "exclusive_energy_budget_with_binding_residual_kev",
                                  "exclusive_energy_budget_with_binding_residual_fraction", "phonon_energy_kev"):
                        budget[field] = None
                    # Preserve one available native column, while refusing to
                    # compute the other column from only a subset of batches.
                    if repeat == 2:
                        budget["recoil_transfer_diagnostic_columns_kev"][1] = None
                batch = {"job": {"stage": "production", "medium": "baseline", "species": "H",
                                  "repeat": repeat, "energy_kevnr": 10.0, "n_ions": 2,
                                  "job_id": f"channels-{repeat}", "seed": repeat + 100},
                         "qf": 0.5, "eee_kev": 5.0,
                         "endpoints": {"records": records, "projected_range_mm": 1.0},
                         "paths": None, "budget": budget, "clipped": False,
                         "backscattered": 0, "transmitted": 0}
                (output / "batches" / f"{repeat}.json").write_text(json.dumps(batch), encoding="utf-8")
            with mock.patch.object(study.core, "source_fingerprint", return_value={}), \
                 mock.patch.object(study.core, "source_control_fingerprint", return_value={}):
                validation = study.analyse(output, config, output / "unused-source")
            with (output / "summary.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        return rows[0], validation

    def test_channel_and_independent_residual_sems_preserve_batch_covariance(self):
        row, _ = self.analyse_channels()
        expected = {
            "primary_ionization_kev": (4.0, 1 / math.sqrt(3)),
            "secondary_ionization_kev": (1.0, 1 / math.sqrt(3)),
            "phonon_kev": (4.0, 0.5 / math.sqrt(3)),
            "remaining_energy_kev": (5.0, 0.0),
            "binding_energy_sink_kev": (0.2, 0.1 / math.sqrt(3)),
            "exclusive_energy_budget_residual_kev": (1.0, 0.5 / math.sqrt(3)),
            "exclusive_energy_budget_with_binding_residual_kev": (0.8, 0.4 / math.sqrt(3)),
            "recoil_transfer_diagnostic_column_1_kev": (3.3, 0.3 / math.sqrt(3)),
            "recoil_transfer_diagnostic_column_2_kev": (1.2, 0.2 / math.sqrt(3)),
        }
        for field, (mean, sem) in expected.items():
            with self.subTest(channel=field):
                self.assertAlmostEqual(float(row[field + "_mean"]), mean)
                self.assertAlmostEqual(float(row[field + "_sem"]), sem)
        self.assertAlmostEqual(float(row["eee_srim_kev_sem"]), 0)
        self.assertEqual(float(row["lateral_straggling_mm_sem"]), 0)
        self.assertEqual(row["recoil_transfer_diagnostic_column_count"], "2")
        self.assertIn("excluded from terminal energy sum", row["recoil_transfer_diagnostic_interpretation"])
        # The primary/secondary variations cancel, rather than adding in
        # quadrature; neither recoil-transfer column enters this residual.
        self.assertAlmostEqual(float(row["energy_closure_fraction_mean"]), 0.92)
        self.assertAlmostEqual(float(row["energy_closure_fraction_sem"]), 0.04 / math.sqrt(3))

    def test_unknown_channels_and_incomplete_native_columns_remain_null(self):
        row, validation = self.analyse_channels(missing=True)
        for field in ("phonon_kev", "binding_energy_sink_kev", "exclusive_energy_budget_residual_kev",
                      "exclusive_energy_budget_with_binding_residual_kev", "recoil_transfer_diagnostic_column_2_kev",
                      "energy_closure_fraction"):
            with self.subTest(channel=field):
                self.assertEqual(row[field + "_mean"], "")
                self.assertEqual(row[field + "_sem"], "")
        self.assertAlmostEqual(float(row["recoil_transfer_diagnostic_column_1_kev_mean"]), 3.3)
        self.assertEqual(row["energy_budget_valid"], "False")
        self.assertEqual(row["energy_budget_relative_residual_max_abs"], "")
        self.assertFalse(validation["passed"])


if __name__ == "__main__":
    unittest.main()
