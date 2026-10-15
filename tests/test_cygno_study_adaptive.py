"""Adaptive planning tests use synthetic histories; no simulations are run."""

from copy import deepcopy
import math
import unittest

from scripts.cygno_study_adaptive import (
    adaptive_repeats,
    convergence_diagnostics,
    refinement_diagnostics,
    refinement_points,
)


CONFIG = {
    "simulation": {"independent_batches": 3, "maximum_batches": 9,
                   "ions_per_batch": 100, "displacement_energy_ev": 21.0,
                   "exyz_energy_samples": 1000},
    "validation": {"maximum_qf_relative_sem": 0.02,
                   "maximum_range_relative_sem": 0.03},
}


def pilot(energy, ee, *, species="He", medium="baseline", repeat=0, cascade=5):
    return {"job": {"stage": "pilot", "medium": medium, "species": species,
                    "energy_kevnr": energy, "repeat": repeat},
            "eee_kev": ee, "effective_settings": {"cascade_type": cascade},
            "clipped": False, "backscattered": 0, "transmitted": 0}


def production(repeat, medium, *, qf=0.5, positions=(0.99, 1.01), species="He", energy=10):
    return {"job": {"stage": "production", "medium": medium, "species": species,
                    "energy_kevnr": energy, "repeat": repeat, "n_ions": len(positions)},
            "qf": qf, "endpoints": {"records": [
                {"projected_displacement_mm": value} for value in positions]}}


def complete_point(qf_values=(0.5, 0.5, 0.5), positions=(0.99, 1.01)):
    return [production(repeat, medium, qf=qf, positions=positions)
            for repeat, qf in enumerate(qf_values)
            for medium in ("baseline", "contaminated")]


class RefinementTests(unittest.TestCase):
    def test_bracketed_log_conversion_proposes_energies_only(self):
        grid = [0.1, 1, 10, 100, 1000, 3000]
        batches = [pilot(energy, 0.5 * energy) for energy in grid]
        before = deepcopy(batches)
        result = refinement_points(batches, grid)
        self.assertEqual(result, {"He": [0.5, 2000.0]})
        self.assertEqual(batches, before)
        self.assertTrue(all(isinstance(value, float) for value in result["He"]))
        self.assertEqual(refinement_diagnostics(batches, grid)[0]["flags"], [])

    def test_nonmonotonic_direct_means_are_retained_as_unresolved(self):
        batches = [pilot(1, 1), pilot(10, 0.8), pilot(100, 3)]
        self.assertEqual(refinement_points(batches, [1, 10, 100]), {"He": []})
        flags = refinement_diagnostics(batches, [1, 10, 100])[0]["flags"]
        self.assertIn("nonmonotone_pilot_electronic_energy", flags)

    def test_unbracketed_boundaries_are_not_extrapolated(self):
        batches = [pilot(1, 0.5), pilot(10, 5)]
        self.assertEqual(refinement_points(batches, [1, 10]), {"He": []})
        flags = refinement_diagnostics(batches, [1, 10])[0]["flags"]
        self.assertIn("unbracketed_electronic_energy_0.25_kev", flags)
        self.assertIn("unbracketed_electronic_energy_1000_kev", flags)

    def test_actual_three_point_curvature_proposes_adjacent_log_midpoints(self):
        batches = [pilot(1, 0.1), pilot(10, 2), pilot(100, 10)]
        result = refinement_points(batches, [1, 10, 100], requested_eee=(0.1, 10))
        self.assertEqual(len(result["He"]), 2)
        self.assertAlmostEqual(result["He"][0], math.sqrt(10), places=7)
        self.assertAlmostEqual(result["He"][1], math.sqrt(1000), places=6)
        flags = refinement_diagnostics(batches, [1, 10, 100], (0.1, 10))[0]["flags"]
        self.assertIn("curvature_screen_has_no_independent_pilot_sem", flags)

    def test_curvature_with_large_replicate_uncertainty_is_not_claimed_resolved(self):
        batches = [pilot(1, 0.1, repeat=index) for index in range(2)]
        batches += [pilot(10, value, repeat=index) for index, value in enumerate([1, 3])]
        batches += [pilot(100, 10, repeat=index) for index in range(2)]
        self.assertEqual(refinement_points(batches, [1, 10, 100], (0.1, 10)), {"He": []})
        flags = refinement_diagnostics(batches, [1, 10, 100], (0.1, 10))[0]["flags"]
        self.assertIn("curvature_not_resolved_above_pilot_uncertainty", flags)

    def test_quick_cascades_and_contaminated_medium_cannot_define_baseline_response(self):
        batches = [pilot(1, 0.5), pilot(10, 5), pilot(100, 90, cascade=1),
                   pilot(1000, 900, medium="contaminated")]
        result = refinement_points(batches, [0.5, 1, 10])
        self.assertEqual(result, {"He": []})
        self.assertIn("invalid_or_unverified_full_cascade_pilot",
                      refinement_diagnostics(batches, [0.5, 1, 10])[0]["flags"])

    def test_existing_boundaries_and_species_specific_grids_are_not_duplicated(self):
        grid = [0.1, 1, 10, 100, 1000, 3000]
        batches = [pilot(energy, 0.5 * energy, species=species)
                   for species in ("He", "O") for energy in grid]
        result = refinement_points(batches, {"He": [0.500000001, 2000], "O": [0.1, 3000]})
        self.assertEqual(result, {"He": [], "O": [0.5, 2000.0]})


class AdaptiveRepeatTests(unittest.TestCase):
    def test_three_precise_batches_require_no_extra_events(self):
        batches = complete_point()
        self.assertEqual(adaptive_repeats(batches, CONFIG), [])
        diagnostic = convergence_diagnostics(batches, CONFIG)[0]
        self.assertTrue(diagnostic["converged"])
        self.assertFalse(diagnostic["exhausted"])
        self.assertEqual(diagnostic["flags"], [])

    def test_failed_qf_precision_in_either_medium_adds_one_paired_repeat(self):
        batches = complete_point()
        for batch in batches:
            if batch["job"]["medium"] == "contaminated":
                batch["qf"] = [0.3, 0.5, 0.7][batch["job"]["repeat"]]
        proposed = adaptive_repeats(batches, CONFIG)
        self.assertEqual([(row["medium"], row["repeat"]) for row in proposed],
                         [("baseline", 3), ("contaminated", 3)])
        for row in proposed:
            self.assertEqual(row["stage"], "production")
            self.assertEqual(row["n_ions"], 100)
            self.assertEqual(row["displacement_energy_ev"], 21)
            self.assertEqual(row["exyz_energy_samples"], 1000)
            self.assertIn("contaminated:qf_precision_unresolved", row["planning_flags"])

    def test_range_uncertainty_comes_from_event_endpoints_not_batch_mean_scatter(self):
        # Every batch mean is identical, but the pooled event mean is imprecise.
        batches = complete_point(positions=(0, 2))
        proposed = adaptive_repeats(batches, CONFIG)
        self.assertEqual(len(proposed), 2)
        self.assertTrue(all(row["repeat"] == 3 for row in proposed))
        diagnostic = convergence_diagnostics(batches, CONFIG)[0]
        self.assertGreater(diagnostic["media"]["baseline"]["projected_relative_sem"], 0.03)

    def test_incomplete_initial_three_repeats_are_completed_before_extension(self):
        batches = complete_point()[:4]
        proposed = adaptive_repeats(batches, CONFIG)
        self.assertEqual([(row["medium"], row["repeat"]) for row in proposed],
                         [("baseline", 2), ("contaminated", 2)])

    def test_refinement_histories_request_the_next_production_repeat(self):
        batches = complete_point(qf_values=(0.3, 0.5, 0.7))
        for batch in batches:
            batch["job"]["stage"] = "refinement"
        proposed = adaptive_repeats(batches, CONFIG)
        self.assertEqual([(row["medium"], row["repeat"], row["stage"]) for row in proposed],
                         [("baseline", 3, "production"), ("contaminated", 3, "production")])
        batches.extend(production(3, medium, qf=0.3)
                       for medium in ("baseline", "contaminated"))
        self.assertEqual({row["repeat"] for row in adaptive_repeats(batches, CONFIG)}, {4})

    def test_existing_half_pair_is_finished_without_duplicate_completed_job(self):
        batches = complete_point() + [production(3, "baseline")]
        proposed = adaptive_repeats(batches, CONFIG)
        self.assertEqual([(row["medium"], row["repeat"]) for row in proposed], [("contaminated", 3)])

    def test_next_call_advances_one_repeat_and_nine_batch_cap_preserves_unresolved_flag(self):
        batches = complete_point(qf_values=(0.3, 0.5, 0.7))
        self.assertEqual({row["repeat"] for row in adaptive_repeats(batches, CONFIG)}, {3})
        for repeat in range(3, 9):
            batches.extend(production(repeat, medium, qf=0.3 if repeat % 2 else 0.7)
                           for medium in ("baseline", "contaminated"))
            proposed = adaptive_repeats(batches, CONFIG)
            if repeat < 8:
                self.assertEqual({row["repeat"] for row in proposed}, {repeat + 1})
            else:
                self.assertEqual(proposed, [])
        diagnostic = convergence_diagnostics(batches, CONFIG)[0]
        self.assertFalse(diagnostic["converged"])
        self.assertTrue(diagnostic["exhausted"])
        self.assertIn("maximum_independent_batches_exhausted", diagnostic["flags"])

    def test_zero_response_and_nonpositive_projected_means_are_unresolved_without_division(self):
        for positions in ((0, 0), (-1, -2)):
            with self.subTest(positions=positions):
                batches = complete_point(qf_values=(0, 0, 0), positions=positions)
                self.assertEqual(len(adaptive_repeats(batches, CONFIG)), 2)
                diagnostic = convergence_diagnostics(batches, CONFIG)[0]
                self.assertIn("baseline:nonpositive_projected_mean", diagnostic["flags"])
                self.assertIn("baseline:nonpositive_or_missing_qf", diagnostic["flags"])
                self.assertIsNone(diagnostic["media"]["baseline"]["projected_relative_sem"])
                self.assertIsNone(diagnostic["media"]["baseline"]["qf_relative_sem"])

    def test_duplicate_completed_repeat_is_rejected(self):
        batch = production(0, "baseline")
        with self.assertRaisesRegex(ValueError, "Duplicate completed"):
            adaptive_repeats([batch, deepcopy(batch)], CONFIG)

    def test_pilot_batches_do_not_supply_production_convergence(self):
        self.assertEqual(adaptive_repeats([pilot(10, 5)], CONFIG), [])


if __name__ == "__main__":
    unittest.main()
