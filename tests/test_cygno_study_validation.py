import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.cygno_study_validation import analyse_systematics, verify_archives


CONFIG = {
    "study": {"species": ["F"]},
    "simulation": {"displacement_energy_ev": 21, "exyz_energy_samples": 1000},
    "systematics": {"independent_batches": 3, "ions_per_batch": 100,
        "energies_kevnr": [1, 100], "displacement_energies_ev": [10, 30],
        "exyz_refinement_factors": [1, 4]},
    "validation": {"maximum_range_relative_sem": 0.03},
}


def systematic_batches():
    result = []
    for energy in (1, 100):
        for stage, threshold, samples in (("systematics", 10, 1000), ("systematics", 30, 1000),
                ("track_sampling", 21, 1000), ("track_sampling", 21, 4000)):
            for repeat in range(3):
                fine = samples == 4000
                result.append({
                    "job": {"stage": stage, "medium": "baseline", "species": "F",
                        "energy_kevnr": energy, "displacement_energy_ev": threshold,
                        "exyz_energy_samples": samples, "repeat": repeat, "n_ions": 100,
                        "seed": threshold * 100 + repeat},
                    "effective_settings": {"cascade_type": 5},
                    "qf": 0.5 + (threshold - 21) * 0.001 + (repeat - 1) * 0.01,
                    "exyz_step_ev": 1 if energy == 1 else 25 if fine else 100,
                    "paths": {"sampled_primary_polyline_length_mean_mm": (1 + repeat * 0.01) * (1.005 if fine and energy == 100 else 1)},
                })
    return result


class SystematicsTests(unittest.TestCase):
    def test_real_batch_statistics_reference_and_sampling_floor(self):
        result = analyse_systematics(systematic_batches(), CONFIG)
        groups = [row for row in result["rows"] if row["comparison"] == "group"]
        self.assertEqual(len(groups), 8)
        self.assertTrue(all(row["complete"] for row in groups))
        threshold = next(row for row in result["rows"] if row["comparison"] == "threshold_vs_nominal" and row["energy_kevnr"] == 100 and row["displacement_energy_ev"] == 10)
        self.assertAlmostEqual(threshold["qf_difference"], -0.011)
        self.assertGreater(threshold["qf_difference_sem"], 0)
        floor = next(row for row in result["rows"] if row["comparison"] == "track_refinement" and row["energy_kevnr"] == 1)
        self.assertTrue(floor["same_actual_exyz_increment"])
        self.assertFalse(floor["sampled_polyline_convergence_resolved"])
        fine = next(row for row in result["rows"] if row["comparison"] == "track_refinement" and row["energy_kevnr"] == 100)
        self.assertAlmostEqual(fine["paired_relative_path_change_mean"], 0.005)
        self.assertEqual(fine["n_paired_batches"], 3)
        self.assertTrue(fine["sampled_polyline_convergence_resolved"])
        self.assertIsNone(fine["actual_primary_trajectory_length_mm"])
        self.assertIsNone(fine["total_cascade_trajectory_length_mm"])
        self.assertTrue(any(check["severity"] == "limitation" for check in result["checks"]))

    def test_missing_batches_and_single_batch_uncertainty_stay_unresolved(self):
        result = analyse_systematics(systematic_batches()[:1], CONFIG)
        first = result["rows"][0]
        self.assertEqual(first["n_batches"], 1)
        self.assertIsNone(first["qf_sem"])
        self.assertFalse(first["complete"])
        self.assertTrue(any(not check["passed"] and check["severity"] == "failure" for check in result["checks"]))

    def test_unmatched_seeds_cannot_supply_paired_uncertainty(self):
        batches = systematic_batches()
        for batch in batches:
            if batch["job"]["exyz_energy_samples"] == 4000:
                batch["job"]["seed"] += 1000
        result = analyse_systematics(batches, CONFIG)
        comparisons = [row for row in result["rows"] if row["comparison"] == "track_refinement"]
        self.assertTrue(all(row["n_paired_batches"] == 0 for row in comparisons))
        self.assertTrue(all(row["paired_relative_path_change_sem"] is None for row in comparisons))
        self.assertTrue(all(not row["complete"] for row in comparisons))


def make_archive(root, *, unsafe_name=None, incorrect_manifest=False):
    path = root / "raw" / "batch.tar.gz"
    path.parent.mkdir()
    payload = b"raw SRIM input and output\r\n"
    digest = hashlib.sha256(payload).hexdigest()
    hashes = {"TRIM.IN": digest}
    manifest = {"job": {"job_id": "test-job"}, "sha256": {} if incorrect_manifest else hashes}
    with tarfile.open(path, "w:gz") as handle:
        for name, content in ((unsafe_name or "TRIM.IN", payload), ("manifest.json", json.dumps(manifest).encode())):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            handle.addfile(info, io.BytesIO(content))
    return {"job": {"job_id": "test-job"}, "raw_archive": "raw/batch.tar.gz", "raw_sha256": hashes}


class ArchiveTests(unittest.TestCase):
    def test_content_and_both_hash_maps_verified_without_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = make_archive(root)
            result = verify_archives(root, [batch])
            self.assertTrue(result["passed"])
            self.assertEqual(result["verified_archives"], 1)
            self.assertFalse((root / "TRIM.IN").exists())
            batch["raw_sha256"]["TRIM.IN"] = "0" * 64
            corrupted = verify_archives(root, [batch])
            self.assertFalse(corrupted["passed"])
            self.assertIn("hash mismatch", corrupted["rows"][0]["detail"])

    def test_traversal_and_manifest_disagreement_are_rejected(self):
        for unsafe, wrong_manifest, phrase in (("../outside", False, "Unsafe"), (None, True, "manifest")):
            with self.subTest(unsafe=unsafe, wrong_manifest=wrong_manifest):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    batch = make_archive(root, unsafe_name=unsafe, incorrect_manifest=wrong_manifest)
                    result = verify_archives(root, [batch])
                    self.assertFalse(result["passed"])
                    self.assertIn(phrase, result["rows"][0]["detail"])
                    self.assertFalse((root.parent / "outside").exists())

    def test_missing_archives_and_empty_campaign_are_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = {"job": {"job_id": "missing"}, "raw_archive": "raw/missing.tar.gz", "raw_sha256": {"x": "0" * 64}}
            self.assertFalse(verify_archives(root, [missing])["passed"])
            empty = verify_archives(root, [])
            self.assertFalse(empty["passed"])
            self.assertEqual(empty["verified_archives"], 0)


if __name__ == "__main__":
    unittest.main()
