import math
from pathlib import Path
import tempfile
import unittest

from scripts.cygno_study_physics import (
    configure_trim_diskfiles,
    configure_bulk_energy_ceiling,
    integrate_energy_budget,
    integrate_native_profile,
    parse_exyz,
    parse_effective_settings,
    parse_range_3d,
    write_bulk_trim_dat,
)


EFFECTIVE_SETTINGS = '''Saved DATA File for TRIM
ION DATA:At.Numb, Mass(amu), Energy (keV) , Angle to Target
          6     12.0000  1.0000E+00    00.0
ION NUMB: Current, Total, AutoSave, Transmitted, Backscattered
        1000 1000 1000 0 0
Cascade Type, FileFLAGS: Sputter, Ranges, Backscatter, Transmit, Collisions, Random Seeds(2)
 5 1 1 1 1 0 0 100 200
TGT DATA: ------- Numb.Elements, Numb.Layers -----------------
                       2           1
TGT DATA: Atomic Numb, Mass, Disp., Binding, Surf. Energy(eV)
 2 4.0030 21.00 0.00 0.00
 6 12.0110 21.00 2.00 4.00
TGT Layers: Name Width(A), Depth(A), Density(g/cm3), Stoichiometry (Tgt.Atoms)
 "test gas" 2.0000E+08 2.0000E+08 1.3892E-03 0.6 0.4
Lowest E, Ed(min) (eV)
 1 21
Total Sputtered Atoms and Energy (eV) (Target Atoms 1 - 2)
 0.000E+000 0.000E+000 0.000E+000 0.000E+000
'''


class BulkInputTests(unittest.TestCase):
    def test_bulk_energy_ceiling_prevents_legacy_rounding_rejection(self):
        requested = 0.21460439
        old_energy = f"{requested:.6g}"
        self.assertLess(float(old_energy), requested)
        original = ("Ion: Z1 , M1, Energy (keV), Angle,Number,Bragg Corr,AutoSave Number.\n"
                    f"6 12.00000 {old_energy} 0 100 0 100\n"
                    "Cascade Type[1=None;2=Full;4-5=Ions(TRIM.dat)], Random Number Seed, Reminders\n"
                    "5 12345 0\n"
                    "unchanged target section\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TRIM.IN"
            path.write_text(original, encoding="ascii")
            metadata = configure_bulk_energy_ceiling(path, requested)
            rows = path.read_text(encoding="ascii").splitlines()
            fields = rows[1].split()
            self.assertGreater(float(fields[2]), requested)
            self.assertEqual(fields[:2] + fields[3:], ["6", "12.00000", "0", "100", "0", "100"])
            self.assertEqual(rows[2:], original.splitlines()[2:])
            self.assertEqual(metadata["requested_energy_kev"], requested)
            self.assertEqual(metadata["max_energy_kev"], float(fields[2]))
            self.assertEqual(metadata["relative_margin"], 1e-5)
            self.assertTrue(path.read_bytes().endswith(b"\r\n"))

    def test_all_log_grid_dat_energies_fit_ceiling_without_changing_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, dat = root / "TRIM.IN", root / "TRIM.dat"
            for index in range(28):
                energy = float(f"{0.1 * (3000 / 0.1) ** (index / 27):.8g}")
                path.write_text(f"Ion: data\n9 18.99840 {energy:.6g} 0 1 0 1\nCascade Type\n5 12345 0\n", encoding="ascii")
                write_bulk_trim_dat(dat, 9, energy, 1, 1e8)
                event_bytes = dat.read_bytes()
                metadata = configure_bulk_energy_ceiling(path, energy)
                actual_dat_kev = float(dat.read_text(encoding="ascii").splitlines()[10].split()[2]) / 1000
                self.assertLessEqual(actual_dat_kev, metadata["max_energy_kev"])
                self.assertEqual(dat.read_bytes(), event_bytes)

    def test_bulk_ceiling_rejects_other_modes_invalid_numbers_and_ambiguous_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TRIM.IN"
            source = "Ion: data\n6 12 1 0 1 0 1\nCascade Type\n5 12345 0\n"
            for energy, margin in ((float("nan"), 1e-5), (0, 1e-5), (1, -1e-5), (1, float("inf"))):
                path.write_text(source, encoding="ascii")
                with self.assertRaises(ValueError):
                    configure_bulk_energy_ceiling(path, energy, margin)
                self.assertEqual(path.read_text(encoding="ascii"), source)
            for malformed in (source.replace("5 12345", "2 12345"), source + "Ion: duplicate\n6 12 1 0 1 0 1\n"):
                path.write_text(malformed, encoding="ascii")
                with self.assertRaises(ValueError):
                    configure_bulk_energy_ceiling(path, 1)
                self.assertEqual(path.read_text(encoding="ascii"), malformed)

    def test_bulk_events_use_documented_units_header_and_dos_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TRIM.dat"
            metadata = write_bulk_trim_dat(path, 8, 12.5, 2, 123456.0, "oxygen bulk recoil")
            raw = path.read_bytes()
        self.assertTrue(raw.endswith(b"\r\n\x1a"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        lines = raw[:-1].decode("ascii").splitlines()
        self.assertEqual(lines[7], "oxygen bulk recoil")
        self.assertEqual(len(lines), 12)
        self.assertEqual(lines[10].split(), ["00001", "8", "12500", "123456", "0", "0", "1", "0", "0"])
        self.assertEqual(metadata["cascade_type"], 5)

    def test_diskfile_edit_keeps_previous_and_following_sections(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TRIM.IN"
            path.write_text("before\nDiskfiles (0=no,1=yes):\n1 1 1 0 0 0\nafter\n", encoding="ascii")
            settings = configure_trim_diskfiles(path, exyz_step_ev=10)
            self.assertEqual(path.read_bytes(), b"before\r\nDiskfiles (0=no,1=yes):\r\n1 1 1 1 0 10\r\nafter\r\n")
            self.assertEqual(settings["exyz_step_ev"], 10)
            path.write_text("before\n1 1 1 0 0 0\nafter\n", encoding="ascii")
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                configure_trim_diskfiles(path, exyz_step_ev=10)
            self.assertEqual(path.read_bytes(), before)


class RangeAndTrajectoryTests(unittest.TestCase):
    def test_native_initial_energy_comparison_uses_printed_decimal_precision(self):
        examples = [(0.21460439, "2.1460E-01", 1e-5), (14.310374, "1.4310E+01", 1e-3)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXYZ.txt"
            for requested, printed, quantum in examples:
                with self.subTest(requested=requested):
                    path.write_text(f"Energy (keV) Depth (Angstrom) Y Z\n1 {printed} 1000 0 0 0.1\n", encoding="ascii")
                    track = parse_exyz(path, initial_energy_kev=requested)["tracks"][0]
                    self.assertTrue(track["starts_at_initial_energy"])
                    self.assertEqual(track["first_sample_energy_text"], printed)
                    self.assertAlmostEqual(track["first_sample_energy_print_quantum_kev"], quantum)
                    self.assertAlmostEqual(track["initial_energy_comparison_tolerance_kev"], quantum / 2, delta=1e-10)
                    ceiling_printed = f"{requested * 1.001:.4E}"
                    path.write_text(f"Energy (keV) Depth (Angstrom) Y Z\n1 {ceiling_printed} 1000 0 0 0.1\n", encoding="ascii")
                    ceiling_track = parse_exyz(path, initial_energy_kev=requested)["tracks"][0]
                    self.assertFalse(ceiling_track["starts_at_initial_energy"])

    def test_source_offset_and_displacement_are_distinct_from_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            endpoints_path = root / "RANGE_3D.txt"
            endpoints_path.write_text("Final Ion Positions\n(Angstrom) (Angstrom) (Angstrom)\n0000001 1030 40 0\n0000002 1010 -20 0\n", encoding="ascii")
            endpoints = parse_range_3d(endpoints_path, start_depth_angstrom=1000)
            self.assertAlmostEqual(endpoints["projected_range_mm"], 20e-7)
            self.assertAlmostEqual(endpoints["projected_range_sem_mm"], 10e-7)
            self.assertAlmostEqual(endpoints["records"][0]["endpoint_displacement_3d_mm"], 50e-7)
            self.assertIsNone(endpoints["actual_trajectory_length_mm"])
            exyz_path = root / "EXYZ.txt"
            exyz_path.write_text("Energy (keV) Depth (Angstrom) Y Z\n0000001 1.0 1000 0 0 0.1 0\n0000001 0.5 1030 0 0 0.1 5\n", encoding="ascii")
            parsed = parse_exyz(exyz_path, start_depth_angstrom=1000, endpoints=endpoints, initial_energy_kev=1)
            self.assertAlmostEqual(parsed["tracks"][0]["sampled_primary_polyline_length_mm"], 70e-7)
            self.assertTrue(parsed["tracks"][0]["starts_at_initial_energy"])
            self.assertIsNone(parsed["actual_primary_trajectory_length_mm"])
            self.assertGreater(parsed["tracks"][0]["sampled_primary_polyline_length_mm"], endpoints["records"][0]["endpoint_displacement_3d_mm"])

    def test_endpoint_duplicates_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "RANGE_3D.txt"
            path.write_text("Angstrom\n1 1 2 3\n1 2 3 4\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                parse_range_3d(path)

    def test_native_exyz_six_field_initial_row_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXYZ.txt"
            path.write_text(
                "Energy (keV) Depth (Angstrom) Y Z Se LastRecoil\n"
                "0000001 1.0000E+02 1.0000E+06 0.0000E+00 0.0000E+00 2.0000E-02\n"
                "0000001 9.0000E+01 1.0003E+06 4.0000E+02 0.0000E+00 1.0000E-02 5.0000E+00\n",
                encoding="ascii",
            )
            result = parse_exyz(path, start_depth_angstrom=1e6, initial_energy_kev=100)
            track = result["tracks"][0]
            self.assertEqual(track["n_samples"], 2)
            self.assertTrue(track["starts_at_initial_energy"])
            self.assertIsNone(track["samples"][0]["last_recoil_transfer_ev"])
            self.assertEqual(track["samples"][1]["last_recoil_transfer_ev"], 5.0)
            self.assertAlmostEqual(track["sampled_primary_polyline_length_mm"], 500e-7)


class BudgetTests(unittest.TestCase):
    def test_effective_secondary_escape_count_energy_pairs_and_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TDATA.sav"
            escaping = EFFECTIVE_SETTINGS.replace(
                "0.000E+000 0.000E+000 0.000E+000 0.000E+000",
                "2.000E+000 1.200E+001 3.000E+000 4.500E+001")
            path.write_text(escaping, encoding="ascii")
            result = parse_effective_settings(path)
            self.assertEqual(result["target_escape_by_element"]["2"]["count"], 2)
            self.assertEqual(result["target_escape_by_element"]["6"]["total_energy_ev"], 45)
            self.assertEqual(result["target_escape_count"], 5)
            self.assertEqual(result["target_escape_total_energy_ev"], 57)
            self.assertIn("not normalized per ion", result["target_escape_interpretation"])
            path.write_text(escaping.replace("3.000E+000 4.500E+001", "3.000E+000 4.500E+001 6.000E+000"), encoding="ascii")
            with self.assertRaisesRegex(ValueError, "pair per element"):
                parse_effective_settings(path)

    def test_effective_settings_and_documented_phonon_closure_with_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved = root / "TDATA.sav"
            saved.write_text(EFFECTIVE_SETTINGS, encoding="ascii")
            effective = parse_effective_settings(saved)
            self.assertAlmostEqual(effective["density_g_cm3"], 1.3892e-3)
            self.assertEqual(effective["target_energy_settings"]["6"]["symbol"], "C")
            self.assertEqual(effective["target_energy_settings"]["6"]["lattice_binding_energy_ev"], 2)
            self.assertEqual(effective["target_element_order_z"], [2, 6])
            self.assertEqual(effective["n_ions_calculated"], 1000)
            self.assertEqual(effective["n_ions_requested"], 1000)
            self.assertEqual(effective["cascade_type"], 5)
            self.assertEqual(effective["ion_mass_u"], 12)
            self.assertIsNone(effective["gas_phase"])
            ioniz = root / "IONIZ.txt"
            ioniz.write_text("Ionization Energy Units are >>>> eV /(Angstrom-Ion) <<<<\n10 20 10\n20 10 0\n30 0 0\n", encoding="ascii")
            phonon = root / "PHONON.txt"
            phonon.write_text("Table Units are >>>> Phonons /(Angstrom-Ion) <<<<\n10 10 15\n20 10 15\n30 0 0\n", encoding="ascii")
            vacancy = root / "VACANCY.txt"
            vacancy.write_text("Table Units are >>>> Vacancies/(Angstrom-Ion) <<<<\n10 1 2 5\n20 0 0 0\n30 0 0 0\n", encoding="ascii")
            budget = integrate_energy_budget(ioniz, phonon, None, 1,
                phonon_energy_convention="srim_documented_energy", vacancy_path=vacancy,
                effective_settings=effective)
            self.assertTrue(budget["phonon_energy_convention_supported"])
            self.assertIn("documented", budget["phonon_energy_convention"])
            self.assertAlmostEqual(budget["phonon_energy_kev"], 0.5)
            self.assertAlmostEqual(budget["binding_energy_sink_kev"], 0.1)
            self.assertAlmostEqual(budget["exclusive_energy_budget_residual_kev"], 0.1)
            self.assertAlmostEqual(budget["exclusive_energy_budget_with_binding_residual_kev"], 0)
            self.assertEqual(budget["binding_energy_sink_by_element"][1]["vacancies_per_ion"], 50)
            self.assertFalse(budget["energy_closure_verified"])

    def test_rectangular_integration_keeps_first_bin_and_secondary_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ioniz = root / "IONIZ.txt"
            ioniz.write_text("Total Ions calculated =1000.00\nIonization Energy Units are >>>> eV /(Angstrom-Ion) <<<<\n10 20 10\n20 10 0\n30 0 0\n", encoding="ascii")
            phonon = root / "PHONON.txt"
            phonon.write_text("Table Units are >>>> Phonons /(Angstrom-Ion) <<<<\n10 10 20\n20 10 20\n30 0 0\n", encoding="ascii")
            transfer = root / "E2RECOIL.txt"
            transfer.write_text("Table Energy Units are >>>> eV /(Angstrom-Ion) <<<<\n10 60 10 20 30\n20 0 0 0 0\n30 0 0 0 0\n", encoding="ascii")
            result = integrate_energy_budget(ioniz, phonon, transfer, 1.0)
            self.assertAlmostEqual(result["primary_electronic_energy_kev"], 0.3)
            self.assertAlmostEqual(result["secondary_electronic_energy_kev"], 0.1)
            self.assertAlmostEqual(result["qf_electronic"], 0.4)
            self.assertAlmostEqual(result["remaining_energy_kev"], 0.6)
            self.assertIsNone(result["phonon_energy_kev"])
            self.assertIsNone(result["exclusive_energy_budget_residual_kev"])
            self.assertFalse(result["energy_closure_verified"])
            self.assertEqual(result["recoil_transfer_diagnostic_columns_kev"], [0.6, 0.1, 0.2, 0.3])
            self.assertEqual(result["ioniz_profile"]["calculated_ions"], 1000)
            conditional = integrate_energy_budget(ioniz, phonon, transfer, 1.0, phonon_values_are_ev=True)
            self.assertAlmostEqual(conditional["phonon_energy_kev"], 0.6)
            self.assertAlmostEqual(conditional["exclusive_energy_budget_residual_kev"], 0)
            self.assertIn("conditional", conditional["phonon_energy_convention"])
            self.assertFalse(conditional["energy_closure_verified"])

    def test_nonuniform_bins_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "IONIZ.txt"
            path.write_text("Ionization Energy Units are >>>> eV /(Angstrom-Ion) <<<<\n10 1 0\n20 1 0\n50 1 0\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "uniformly"):
                integrate_native_profile(path)


if __name__ == "__main__":
    unittest.main()
