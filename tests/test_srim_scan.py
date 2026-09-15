import json
import math
import hashlib
import io
import os
import tarfile
from dataclasses import replace
import tempfile
import time
import unittest
import sys
from pathlib import Path

import srim_scan
from srim_gas_sweep import load_elements
from trim_qf_sweep import parse_ioniz


ROOT = Path(__file__).resolve().parents[1]
SRIM_ROOT = Path(
    os.environ.get(
        "SRIM_ROOT",
        ROOT if (ROOT / "Data" / "ATOMDATA").is_file() else ROOT.parent / "srim-2013",
    )
).resolve()
CONFIG = ROOT / "configs" / "noble_cf4_standard.toml"
FAST_CONFIG = ROOT / "configs" / "noble_cf4_40h.toml"
UBUNTU_CONFIG = ROOT / "configs" / "noble_cf4_40h_ubuntu.toml"
STRICT_UBUNTU_CONFIG = ROOT / "configs" / "noble_cf4_7day_ubuntu.toml"
DOCKER_CONFIG = ROOT / "configs" / "noble_cf4_standard_docker.toml"


class ScanGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = srim_scan.load_config(CONFIG)
        cls.elements, _ = load_elements(SRIM_ROOT / "Data" / "ATOMDATA")
        cls.conditions = srim_scan.generate_conditions(cls.config, cls.elements)

    def test_exact_workload(self):
        self.assertEqual(len(self.conditions), 144)
        self.assertEqual(len(srim_scan.make_srmodule_jobs(self.conditions, self.config)), 432)
        self.assertEqual(len(srim_scan.make_trim_jobs(self.conditions, self.config, "trim")), 43_200)
        self.assertEqual(len(srim_scan.make_trim_jobs(self.conditions, self.config, "convergence")), 324)

    def test_preliminary_workload_and_coverage_order(self):
        config = srim_scan.load_config(ROOT / "configs/noble_cf4_preliminary.toml")
        conditions = srim_scan.generate_conditions(config, self.elements)
        self.assertEqual(len(conditions), 18)
        self.assertEqual(len(srim_scan.make_srmodule_jobs(conditions, config)), 54)
        jobs = srim_scan.make_trim_jobs(conditions, config)
        self.assertEqual(len(jobs), 270)
        self.assertEqual(len({job.job_id for job in jobs}), 270)
        self.assertEqual({job.payload["n_ions"] for job in jobs}, {20})
        self.assertEqual({job.payload["condition"]["family"] for job in jobs[:3]}, {"Ar", "Kr", "Xe"})
        self.assertTrue(all(abs(job.payload["energy_kev"] - 0.1) < 1e-10 for job in jobs[:54]))
        self.assertEqual(srim_scan.make_trim_jobs(conditions, config, "convergence"), [])

    def test_standard_grid_remains_strict(self):
        with self.assertRaises(ValueError):
            srim_scan.validate_config(replace(self.config, noble_percent=(60, 80, 95)))

    def test_timing_pilot_exact_small_grid(self):
        config = srim_scan.load_config(ROOT / "configs/noble_cf4_timing_pilot.toml")
        conditions = srim_scan.generate_conditions(config, self.elements)
        self.assertEqual(config.families, ("Ar", "Xe"))
        self.assertEqual(len(conditions), 4)
        self.assertEqual(len(srim_scan.make_srmodule_jobs(conditions, config)), 12)
        jobs = srim_scan.make_trim_jobs(conditions, config)
        self.assertEqual(len(jobs), 48)
        self.assertEqual({round(job.payload["energy_kev"], 6) for job in jobs}, {1, 10, 100, 1000})
        self.assertEqual(sum(job.payload["n_ions"] for job in jobs), 240)
        self.assertLessEqual(config.workers_default, 15)
        self.assertEqual(srim_scan.make_trim_jobs(conditions, config, "convergence"), [])
        for families in ((), ("Ar", "Ar"), ("He",)):
            with self.subTest(families=families), self.assertRaises(ValueError):
                srim_scan.validate_config(replace(config, families=families))
        with self.assertRaises(ValueError):
            srim_scan.validate_config(replace(self.config, families=("Ar", "Xe")))

    def test_preliminary_rejects_invalid_grid(self):
        config = srim_scan.load_config(ROOT / "configs/noble_cf4_preliminary.toml")
        for changes in ({"noble_percent": ()}, {"noble_percent": (60, 60)},
                        {"pressures_atm": (0.0,)}, {"pressures_atm": (1.0, 1.0)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                srim_scan.validate_config(replace(config, **changes))

    def test_disabled_convergence_ignores_populated_grid(self):
        raw = {**self.config.raw, "convergence": {**self.config.raw["convergence"], "enabled": False}}
        self.assertEqual(srim_scan.make_trim_jobs(self.conditions, replace(self.config, raw=raw), "convergence"), [])

    def test_mixture_stoichiometry(self):
        condition = next(
            item
            for item in self.conditions
            if item.family == "Xe" and item.noble_percent == 60 and item.pressure_atm == 1.0
        )
        self.assertEqual(condition.stoich, {"Xe": 0.6, "C": 0.4, "F": 1.6})
        self.assertAlmostEqual(sum(condition.atomic_fractions.values()), 1.0)
        self.assertAlmostEqual(sum(condition.mass_fractions.values()), 1.0)
        self.assertEqual(set(condition.element_masses_u), {"Xe", "C", "F"})

    def test_ideal_density_scales_with_pressure(self):
        rows = [
            item
            for item in self.conditions
            if item.family == "Kr" and item.noble_percent == 80
        ]
        normalized = [item.density_g_cm3 / item.pressure_atm for item in rows]
        self.assertLess(max(normalized) - min(normalized), 1.0e-15)
        self.assertTrue(all(item.density_g_cm3 > 0 for item in rows))

    def test_stable_job_ids(self):
        first = srim_scan.make_srmodule_jobs(self.conditions, self.config)[0]
        second = srim_scan.make_srmodule_jobs(self.conditions, self.config)[0]
        self.assertEqual(first.job_id, second.job_id)

    def test_timeboxed_workload(self):
        config = srim_scan.load_config(FAST_CONFIG)
        conditions = srim_scan.generate_conditions(config, self.elements)
        self.assertEqual(len(srim_scan.make_trim_jobs(conditions, config, "trim")), 576)
        self.assertEqual(len(srim_scan.make_trim_jobs(conditions, config, "convergence")), 324)
        self.assertEqual(config.trim_ions, 50)
        self.assertEqual(config.trim_pressures, (1.0,))
        self.assertTrue(config.derive_full_grid)
        self.assertEqual(config.campaign_sessions, 5)
        self.assertEqual(config.campaign_session_hours, 8.0)

    def test_ubuntu_campaign_uses_pinned_pysrim_backend(self):
        config = srim_scan.load_config(UBUNTU_CONFIG)
        context = srim_scan.resolve_execution_context(config)
        self.assertEqual(context.backend, "pysrim")
        self.assertTrue(context.headless)
        self.assertEqual(
            context.pysrim_expected_commit,
            "9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2",
        )
        self.assertEqual(srim_scan.resolve_srim_root(None, config), (ROOT.parent / "srim-2013").resolve())
        self.assertEqual(len(srim_scan.make_trim_jobs(self.conditions, config, "trim")), 576)

    def test_strict_ubuntu_campaign_is_fully_direct(self):
        config = srim_scan.load_config(STRICT_UBUNTU_CONFIG)
        conditions = srim_scan.generate_conditions(config, self.elements)
        jobs = srim_scan.make_trim_jobs(conditions, config, "trim")
        self.assertEqual(len(conditions), 144)
        self.assertEqual(len(jobs), 43_200)
        self.assertEqual(sum(int(job.payload["n_ions"]) for job in jobs), 43_200_000)
        self.assertEqual(config.trim_pressures, config.pressures_atm)
        self.assertEqual(config.trim_energy_points, 100)
        self.assertEqual(config.trim_ions, 1000)
        self.assertFalse(config.derive_full_grid)
        self.assertEqual(config.campaign_max_cumulative_hours, 168.0)
        self.assertEqual(config.trim_timeout_s, 172800)
        self.assertEqual(config.convergence_timeout_s, 1209600)
        first = jobs[0]
        self.assertEqual(first.payload["ion"], "Xe")
        self.assertAlmostEqual(float(first.payload["energy_kev"]), 500.0)

    def test_docker_campaign_uses_isolated_pysrim_paths(self):
        config = srim_scan.load_config(DOCKER_CONFIG)
        conditions = srim_scan.generate_conditions(config, self.elements)
        context = srim_scan.resolve_execution_context(config)
        self.assertEqual(config.families, ("Xe", "Kr", "Ar"))
        self.assertEqual(len(conditions), 144)
        self.assertEqual(len(srim_scan.make_trim_jobs(conditions, config, "trim")), 43_200)
        self.assertEqual(context.backend, "pysrim")
        self.assertEqual(context.pysrim_root, Path("/workspace/pysrim"))
        self.assertEqual(context.wine_prefix, Path("/data/wine-volume/prefix"))
        self.assertEqual(
            context.pysrim_expected_commit,
            "9b8e1b7e91fe65bf17e9366e1fd9b9406ad125d2",
        )
        self.assertEqual(
            srim_scan.resolve_srim_root(None, config),
            Path("/data/srim-volume/srim-2013"),
        )

    def test_pysrim_payload_uses_installed_atomdata_masses(self):
        condition = self.conditions[0]
        payload = srim_scan.pysrim_element_payload(condition, self.config, self.elements)
        self.assertEqual([row["symbol"] for row in payload], ["C", "F", "Xe"])
        self.assertEqual(payload[-1]["mass_u"], self.elements["Xe"].mass_u)
        self.assertAlmostEqual(sum(float(row["stoich"]) for row in payload), 2.6)

    def test_target_width_contains_prediction_and_monolayers(self):
        condition = self.conditions[0]
        prediction = {"projected_range_cm": "0.01", "longitudinal_straggling_cm": "0.002"}
        width_a = srim_scan.target_width_angstrom(condition, prediction, self.config)
        physics_cm = self.config.target_width_factor * (
            0.01 + self.config.target_sigma_margin * 0.002
        )
        self.assertGreaterEqual(width_a, physics_cm * 1.0e8)
        monolayer_cm = condition.atom_number_density_cm3 ** (-1.0 / 3.0)
        self.assertGreaterEqual(width_a, self.config.minimum_monolayers * monolayer_cm * 1.0e8)


class ParserTests(unittest.TestCase):
    def test_log_energy_anchor_interpolation(self):
        lower = {"energy_kev": 1.0, "qf": 0.1, "range": 1.0}
        upper = {"energy_kev": 100.0, "qf": 0.3, "range": 100.0}
        self.assertAlmostEqual(
            srim_scan.interpolate_anchor_value(lower, upper, 10.0, "qf"), 0.2
        )
        self.assertAlmostEqual(
            srim_scan.interpolate_anchor_value(lower, upper, 10.0, "range", True), 10.0
        )

    def test_range_header_parser(self):
        text = """
 Total Ions calculated =1000.00
 Ion Average Range = 665.6E+01 A   Straggling = 320.8E+01 A
 Ion Lateral Range = 320.5E+01 A   Straggling = 391.8E+01 A
 Ion Radial  Range = 497.8E+01 A   Straggling = 261.8E+01 A
 Range Skewness = 0.2320
 Range Kurtosis = 2.2475
 Total Backscattered Ions= 2
 Transmitted Ions =; Backscattered Ions =2
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "RANGE.txt"
            path.write_text(text, encoding="latin-1")
            parsed = srim_scan.parse_range_summary(path)
        self.assertAlmostEqual(parsed.projected_range_mm, 0.0006656)
        self.assertAlmostEqual(parsed.longitudinal_straggling_mm, 0.0003208)
        self.assertAlmostEqual(parsed.lateral_range_mm, 0.0003205)
        self.assertAlmostEqual(parsed.radial_range_mm, 0.0004978)
        self.assertEqual(parsed.backscattered_ions, 2)
        self.assertEqual(parsed.transmitted_ions, 0)

    def test_target_clipping_detection_covers_profile_escape_and_range_tail(self):
        clear = srim_scan.RangeSummary(
            projected_range_mm=0.001,
            longitudinal_straggling_mm=0.0001,
            lateral_range_mm=None,
            lateral_straggling_mm=None,
            radial_range_mm=None,
            radial_straggling_mm=None,
            skewness=None,
            kurtosis=None,
            backscattered_ions=0,
            transmitted_ions=0,
        )
        self.assertFalse(srim_scan.trim_result_is_clipped(100.0, clear, 100_000.0, 0.98))
        self.assertTrue(srim_scan.trim_result_is_clipped(98_000.0, clear, 100_000.0, 0.98))
        self.assertTrue(
            srim_scan.trim_result_is_clipped(
                100.0, replace(clear, transmitted_ions=1), 100_000.0, 0.98
            )
        )
        long_tail = replace(
            clear, projected_range_mm=0.009, longitudinal_straggling_mm=0.002
        )
        self.assertTrue(
            srim_scan.trim_result_is_clipped(100.0, long_tail, 100_000.0, 0.98)
        )

    def test_raw_archive_manifest_and_hash(self):
        output = b"SRModule output\n"
        manifest = {
            "job_id": "sr-test",
            "stage": "srmodule",
            "raw_output_sha256": hashlib.sha256(output).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.tar.gz"
            with tarfile.open(path, "w:gz") as bundle:
                for name, payload in (
                    ("manifest.json", json.dumps(manifest).encode()),
                    ("SR.IN", b"input\n"),
                    ("SRModule-output.txt", output),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    bundle.addfile(info, io.BytesIO(payload))
            error = srim_scan.validate_raw_archive(
                path, "sr-test", "srmodule", ("SR.IN", "SRModule-output.txt")
            )
        self.assertIsNone(error)

    def test_escape_record_counts(self):
        text = """header\nB  153 54 1.1 0 0 0 -1 0 0\nB  981 54 2.0 0 0 0 -1 0 0\n"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BACKSCAT.txt"
            path.write_text(text, encoding="latin-1")
            self.assertEqual(srim_scan.count_escape_records(path, "B"), 2)
            self.assertEqual(srim_scan.count_escape_records(path, "T"), 0)

    def test_ioniz_integration(self):
        text = """
 Ionization Energy Units are >>>> eV /(Angstrom-Ion) <<<<
 DEPTH IONS RECOILS
----------- ----------- -----------
 1.0E+01 2.0E+00 1.0E+00
 2.0E+01 1.0E+00 0.0E+00
 3.0E+01 0.0E+00 0.0E+00
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "IONIZ.txt"
            path.write_text(text, encoding="latin-1")
            parsed = parse_ioniz(path, 1.0)
        self.assertAlmostEqual(parsed.total_ionization_energy_kev, 0.04)
        self.assertAlmostEqual(parsed.qf_ioniz, 0.04)

    def test_numeric_profile_selects_longest_table(self):
        text = """Layer 1 Density 1.0
----------- ----------- -----------
10 2 1
20 1 0
30 0 0
footer
1 2
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.txt"
            path.write_text(text, encoding="latin-1")
            rows = srim_scan.longest_numeric_table(path)
        self.assertEqual(rows, [[10.0, 2.0, 1.0], [20.0, 1.0, 0.0], [30.0, 0.0, 0.0]])


class CheckpointTests(unittest.TestCase):
    def test_interrupted_inflight_job_is_immediately_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            connection = srim_scan.open_database(run_dir)
            job = srim_scan.JobSpec("interrupt-job", "trim", {"energy_kev": 1.0})

            def interrupt(_job, _runtime):
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                srim_scan.execute_stage(
                    [job],
                    "trim",
                    connection,
                    run_dir,
                    [run_dir / "worker"],
                    interrupt,
                    True,
                    3,
                    None,
                )
            self.assertEqual(srim_scan.job_status(connection, job.job_id)[0], "pending")
            connection.close()

    def test_cumulative_campaign_budget_survives_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = srim_scan.open_database(Path(directory))
            first, usage = srim_scan.begin_campaign_session(connection, 2.0, now_epoch=1000.0)
            self.assertIsNotNone(first)
            self.assertEqual(usage["elapsed_seconds"], 0.0)
            srim_scan.finish_campaign_session(connection, first, now_epoch=2800.0)
            second, usage = srim_scan.begin_campaign_session(connection, 2.0, now_epoch=3000.0)
            self.assertIsNotNone(second)
            self.assertEqual(usage["elapsed_seconds"], 1800.0)
            # Simulate an ungraceful stop. The next restart charges the entire
            # interval conservatively before opening a fresh session.
            third, usage = srim_scan.begin_campaign_session(connection, 2.0, now_epoch=8400.0)
            self.assertIsNone(third)
            self.assertGreaterEqual(usage["elapsed_seconds"], 7200.0)
            connection.close()

    def test_launch_command_honors_interrupt_signal(self):
        srim_scan.SHUTDOWN_REQUESTED.set()
        try:
            with self.assertRaisesRegex(RuntimeError, "stopped after an interrupt"):
                srim_scan.launch_command(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    ROOT,
                    60,
                    process_group=True,
                )
        finally:
            srim_scan.SHUTDOWN_REQUESTED.clear()

    def test_wall_budget_leaves_unstarted_jobs_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            connection = srim_scan.open_database(run_dir)
            job = srim_scan.JobSpec("budget-job", "trim", {"energy_kev": 1.0})

            def should_not_run(_job, _runtime):
                self.fail("worker ran after the launch deadline")

            exhausted = srim_scan.execute_stage(
                [job],
                "trim",
                connection,
                run_dir,
                [run_dir / "worker"],
                should_not_run,
                True,
                3,
                None,
                time.monotonic() - 1.0,
            )
            self.assertTrue(exhausted)
            self.assertEqual(srim_scan.job_status(connection, job.job_id)[0], "pending")
            connection.close()

    def test_pressure_and_energy_derivation(self):
        base = srim_scan.load_config(FAST_CONFIG)
        config = replace(
            base,
            families=("Xe",),
            noble_percent=(60,),
            pressures_atm=(0.5, 1.0),
            energy_points=3,
            trim_energy_points=2,
            trim_pressures=(1.0,),
        )
        elements, _ = load_elements(SRIM_ROOT / "Data" / "ATOMDATA")
        conditions = srim_scan.generate_conditions(config, elements)
        reference = next(item for item in conditions if item.pressure_atm == 1.0)
        direct = []
        srmodule = []
        target_energies = srim_scan.logspace(0.1, 500.0, 3)
        for ion in ("Xe", "C", "F"):
            for energy, qf, range_mm in ((0.1, 0.1, 1.0), (500.0, 0.3, 100.0)):
                direct.append(
                    {
                        "job_id": f"{ion}-{energy}",
                        "family": "Xe",
                        "noble_percent": 60,
                        "cf4_percent": 40,
                        "condition_id": reference.condition_id,
                        "pressure_atm": 1.0,
                        "density_g_cm3": reference.density_g_cm3,
                        "ion": ion,
                        "energy_kev": energy,
                        "qf_trim_ionization": qf,
                        "primary_ionization_energy_kev": qf * energy * 0.75,
                        "secondary_recoil_ionization_energy_kev": qf * energy * 0.25,
                        "projected_range_mm": range_mm,
                        "longitudinal_straggling_mm": range_mm / 10,
                        "lateral_range_mm": range_mm / 5,
                        "lateral_straggling_mm": range_mm / 10,
                        "radial_range_mm": range_mm / 4,
                        "radial_straggling_mm": range_mm / 10,
                        "profile_max_depth_mm": range_mm * 2,
                        "skewness": 0.0,
                        "kurtosis": 3.0,
                        "backscattered_ions": 0,
                        "transmitted_ions": 0,
                        "raw_archive": f"raw/{ion}-{energy}.tar.gz",
                        "normalized_profiles": f"profiles/{ion}-{energy}.json.gz",
                        "clipped": False,
                    }
                )
            for condition in conditions:
                for energy in target_energies:
                    srmodule.append(
                        {
                            "job_id": f"sr-{condition.condition_id}-{ion}",
                            "condition_id": condition.condition_id,
                            "ion": ion,
                            "energy_kev": energy,
                            "srmodule_electronic_fraction_proxy": 0.2,
                            "projected_range_mm": 10.0 / condition.pressure_atm,
                        }
                    )
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            srim_scan.write_csv_atomic(run_dir / "trim_results.csv", direct)
            srim_scan.write_csv_atomic(run_dir / "srmodule_results.csv", srmodule)
            self.assertIsNotNone(srim_scan.derive_trim_grid(run_dir, conditions, config))
            rows = srim_scan.read_csv_rows(run_dir / "trim_results.csv")
            anchors = srim_scan.read_csv_rows(run_dir / "trim_anchor_results.csv")
        self.assertEqual(len(anchors), 6)
        self.assertEqual(len(rows), 18)
        self.assertEqual(
            {row["derivation_kind"] for row in rows},
            {"direct", "pressure_scaled", "energy_interpolated", "pressure_scaled+energy_interpolated"},
        )
        half_pressure = next(
            row
            for row in rows
            if row["condition_id"].endswith("p0p5atm")
            and row["ion"] == "Xe"
            and abs(float(row["energy_kev"]) - 0.1) < 1e-12
        )
        self.assertAlmostEqual(float(half_pressure["projected_range_mm"]), 2.0)

    def test_database_state_and_atomic_result(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            connection = srim_scan.open_database(run_dir)
            job = srim_scan.JobSpec("test-job", "srmodule", {"value": 1})
            srim_scan.register_jobs(connection, [job])
            self.assertEqual(srim_scan.job_status(connection, job.job_id)[0], "pending")
            srim_scan.set_job_state(connection, job.job_id, "running")
            result = run_dir / "job_results" / "result.json"
            srim_scan.atomic_write_json(result, {"ok": True})
            srim_scan.set_job_state(
                connection, job.job_id, "complete", str(result.relative_to(run_dir))
            )
            self.assertEqual(srim_scan.job_status(connection, job.job_id), ("complete", "job_results/result.json"))
            self.assertEqual(json.loads(result.read_text()), {"ok": True})
            connection.close()


if __name__ == "__main__":
    unittest.main()
