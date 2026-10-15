"""Propose extra CYGNO simulations from direct results, without running them.

Interpolation is used only to choose incident energies. No interpolated QF,
energy deposition, endpoint, or trajectory is emitted as a simulated result.
The separate diagnostic functions retain unresolved coverage/convergence cases,
including those for which the configured maximum statistics are exhausted.
"""

from __future__ import annotations

from collections import defaultdict
import math
import statistics
from typing import Any, Mapping, Sequence


MEDIA = ("baseline", "contaminated")

# Two-sided 95% critical values, rounded to three decimal places, from the
# 0.975 column of the NIST/SEMATECH e-Handbook Student-t table:
# https://www.itl.nist.gov/div898/handbook/eda/section3/eda3672.htm
_STUDENT_T_95 = (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306,
                 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131, 2.120,
                 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064,
                 2.060, 2.056, 2.052, 2.048, 2.045)


def student_t_95_factor(degrees_of_freedom: int) -> float:
    """Return the tabulated t(0.975, df), for integer df=1..29 only."""
    if (isinstance(degrees_of_freedom, bool) or not isinstance(degrees_of_freedom, int)
            or not 1 <= degrees_of_freedom <= len(_STUDENT_T_95)):
        raise ValueError("Student-t 95% lookup supports integer degrees of freedom 1 through 29")
    return _STUDENT_T_95[degrees_of_freedom - 1]


student_t_95 = student_t_95_factor


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sem(values: Sequence[float]) -> float | None:
    return statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None


def _already_planned(energy: float, existing: Sequence[float]) -> bool:
    return any(math.isclose(energy, value, rel_tol=1e-7, abs_tol=1e-12)
               for value in existing)


def _refinement_plan(pilot_batches, production_energies, requested_eee):
    if len(requested_eee) != 2 or not 0 < requested_eee[0] < requested_eee[1]:
        raise ValueError("Requested electronic-energy bounds must be positive and increasing")
    grouped = defaultdict(lambda: defaultdict(list))
    rejected = defaultdict(list)
    for batch in pilot_batches:
        job = batch.get("job", {})
        if job.get("stage") != "pilot" or job.get("medium") != "baseline":
            continue
        species = job.get("species")
        if not species:
            continue
        energy = _finite(job.get("energy_kevnr"))
        ee = _finite(batch.get("eee_kev"))
        cascade = batch.get("effective_settings", {}).get("cascade_type")
        if cascade != 5 or energy is None or energy <= 0 or ee is None or not 0 < ee <= energy * 1.001:
            rejected[species].append("invalid_or_unverified_full_cascade_pilot")
            continue
        if batch.get("clipped") or batch.get("backscattered") or batch.get("transmitted"):
            rejected[species].append("pilot_containment_unresolved")
            continue
        grouped[species][energy].append(ee)

    proposals, diagnostics = {}, []
    for species in sorted(set(grouped) | set(rejected)):
        existing_raw = (production_energies.get(species, [])
                        if isinstance(production_energies, Mapping) else production_energies)
        existing = [float(value) for value in existing_raw
                    if _finite(value) is not None and float(value) > 0]
        points = sorted((energy, statistics.fmean(values), _sem(values))
                        for energy, values in grouped[species].items())
        extra = []
        flags = sorted(set(rejected[species]))

        def add(energy):
            value = float(f"{energy:.8g}")
            if not _already_planned(value, existing + extra):
                extra.append(value)

        if len(points) < 2:
            flags.append("insufficient_positive_pilot_points")
        elif any(left[1] >= right[1] for left, right in zip(points, points[1:])):
            flags.append("nonmonotone_pilot_electronic_energy")
        else:
            for bound in requested_eee:
                bracket = next(((left, right) for left, right in zip(points, points[1:])
                                if left[1] <= bound <= right[1]), None)
                if bracket is None:
                    flags.append(f"unbracketed_electronic_energy_{bound:g}_kev")
                    continue
                left, right = bracket
                weight = math.log(bound / left[1]) / math.log(right[1] / left[1])
                add(math.exp(math.log(left[0]) + weight * math.log(right[0] / left[0])))

            for left, middle, right in zip(points, points[1:], points[2:]):
                weight = math.log(middle[0] / left[0]) / math.log(right[0] / left[0])
                prediction = math.exp((1 - weight) * math.log(left[1]) + weight * math.log(right[1]))
                relative_difference = abs(prediction / middle[1] - 1)
                if relative_difference <= 0.02:
                    continue
                if all(point[2] is not None for point in (left, middle, right)):
                    log_uncertainty = math.sqrt(
                        ((1 - weight) * left[2] / left[1]) ** 2
                        + (weight * right[2] / right[1]) ** 2
                        + (middle[2] / middle[1]) ** 2
                    )
                    if abs(math.log(prediction / middle[1])) <= 2 * log_uncertainty:
                        flags.append("curvature_not_resolved_above_pilot_uncertainty")
                        continue
                else:
                    flags.append("curvature_screen_has_no_independent_pilot_sem")
                add(math.sqrt(left[0] * middle[0]))
                add(math.sqrt(middle[0] * right[0]))
        proposals[species] = sorted(extra)
        diagnostics.append({"species": species, "flags": sorted(set(flags)),
                            "n_direct_pilot_energies": len(points),
                            "proposed_extra_energies_kevnr": proposals[species]})
    return proposals, diagnostics


def refinement_points(pilot_batches, production_energies,
                      requested_eee=(0.25, 1000.0)) -> dict[str, list[float]]:
    """Choose extra incident energies from baseline full-cascade pilot means.

    Only strictly monotone positive direct means support boundary inversion.
    Bracketed boundaries use log-log interpolation; unbracketed boundaries are
    diagnosed without extrapolation. Three measured pilot means can trigger a
    >2% curvature screen and adjacent logarithmic midpoints. When replicate SEMs
    exist the curvature must also exceed two propagated standard errors; an
    unavailable pilot SEM is explicitly diagnosed rather than claimed zero.
    """
    return _refinement_plan(pilot_batches, production_energies, requested_eee)[0]


def refinement_diagnostics(pilot_batches, production_energies,
                           requested_eee=(0.25, 1000.0)) -> list[dict[str, Any]]:
    return _refinement_plan(pilot_batches, production_energies, requested_eee)[1]


def _production_groups(batches):
    groups = defaultdict(lambda: {medium: {} for medium in MEDIA})
    for batch in batches:
        job = batch.get("job", {})
        if job.get("stage") not in ("production", "refinement") or job.get("medium") not in MEDIA:
            continue
        energy = _finite(job.get("energy_kevnr"))
        repeat = job.get("repeat")
        if energy is None or energy <= 0 or not job.get("species"):
            raise ValueError("Completed production batch has invalid species/energy identity")
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 0:
            raise ValueError("Completed production batch has invalid repeat index")
        media = groups[(job["species"], energy)]
        if repeat in media[job["medium"]]:
            raise ValueError("Duplicate completed species/energy/medium/repeat batch")
        media[job["medium"]][repeat] = batch
    return groups


def _convergence(media, cfg):
    simulation = cfg["simulation"]
    validation = cfg.get("validation", {})
    minimum = max(3, int(simulation.get("independent_batches", 3)))
    diagnostics = {}
    for medium in MEDIA:
        batches = list(media[medium].values())
        flags = []
        q_values, endpoints = [], []
        for batch in batches:
            q = _finite(batch.get("qf"))
            if q is None or q <= 0:
                flags.append("nonpositive_or_missing_qf")
            else:
                q_values.append(q)
            records = batch.get("endpoints", {}).get("records", [])
            if not records:
                flags.append("missing_projected_endpoints")
            for record in records:
                value = _finite(record.get("projected_displacement_mm"))
                if value is None:
                    flags.append("invalid_projected_endpoint")
                else:
                    endpoints.append(value)
        q_mean = statistics.fmean(q_values) if q_values else None
        q_sem = _sem(q_values)
        q_relative_sem = q_sem / q_mean if q_mean is not None and q_mean > 0 and q_sem is not None else None
        projected_mean = statistics.fmean(endpoints) if endpoints else None
        projected_sem = _sem(endpoints)
        projected_relative_sem = (projected_sem / projected_mean
                                  if projected_mean is not None and projected_mean > 0
                                  and projected_sem is not None else None)
        if len(batches) < minimum:
            flags.append("insufficient_independent_batches")
        if projected_mean is not None and projected_mean <= 0:
            flags.append("nonpositive_projected_mean")
        if q_relative_sem is None or q_relative_sem > validation.get("maximum_qf_relative_sem", 0.02):
            flags.append("qf_precision_unresolved")
        if projected_relative_sem is None or projected_relative_sem > validation.get("maximum_range_relative_sem", 0.03):
            flags.append("projected_range_precision_unresolved")
        diagnostics[medium] = {"converged": not flags, "flags": sorted(set(flags)),
                               "n_batches": len(batches), "n_endpoints": len(endpoints),
                               "qf_mean": q_mean, "qf_sem": q_sem,
                               "qf_relative_sem": q_relative_sem,
                               "projected_mean_mm": projected_mean,
                               "projected_sem_mm": projected_sem,
                               "projected_relative_sem": projected_relative_sem}
    return diagnostics


def impurity_diagnostic(media, cfg) -> dict[str, Any]:
    """Classify paired QF contrasts against the configured material boundary.

    ``media`` maps each medium to {repeat: completed batch}. Matching seeds
    establish the intended pairing. For delta=0.02, L=wet-0.98*base and
    U=wet-1.02*base include baseline uncertainty and paired covariance. Their
    fixed 95% Student-t intervals classify equivalence or a material change.
    Repeated adaptive looks have nominal pointwise coverage, with no sequential
    or simultaneous coverage guarantee. No physical charge equivalence follows.

    Eligibility includes either medium's requested Eee surrogate interval and
    stays true if any completed, ordered paired prefix (at least the minimum
    batch count) was eligible. This prevents boundary drift from dropping an
    earlier in-domain comparison as more histories are accumulated.
    """
    validation = cfg.get("validation", {})
    confidence = _finite(validation.get("confidence_level", 0.95))
    if confidence is None or not math.isclose(confidence, 0.95, rel_tol=0, abs_tol=1e-12):
        raise ValueError("Paired impurity classification supports confidence_level=0.95 only")
    delta = _finite(validation.get("impurity_material_relative_change"))
    if delta is None and "impurity_material_relative_change" not in validation:
        return {"required": False, "resolved": True, "classification": "not_requested", "flags": []}
    if delta is None or not 0 < delta < 1:
        raise ValueError("Impurity material relative change must lie strictly between zero and one")
    minimum = max(3, int(cfg["simulation"].get("independent_batches", 3)))
    grid = cfg.get("grid", {})
    lo = _finite(grid.get("requested_eee_min_kev", 0.25))
    hi = _finite(grid.get("requested_eee_max_kev", 1000.0))
    if lo is None or hi is None or not 0 < lo < hi:
        raise ValueError("Requested electronic-energy interval is invalid")

    def ee(batch):
        value = _finite(batch.get("eee_kev"))
        if value is not None:
            return value
        energy, qf = _finite(batch.get("job", {}).get("energy_kevnr")), _finite(batch.get("qf"))
        return energy * qf if energy is not None and qf is not None else None

    current_means = {}
    for medium in MEDIA:
        values = [value for batch in media.get(medium, {}).values() if (value := ee(batch)) is not None]
        current_means[medium] = statistics.fmean(values) if values else None
    current_eligible = any(value is not None and lo <= value <= hi for value in current_means.values())
    paired, flags, seeds = [], [], set()
    for repeat in sorted(set(media.get("baseline", {})) & set(media.get("contaminated", {}))):
        base, wet = media["baseline"][repeat], media["contaminated"][repeat]
        seed = base.get("job", {}).get("seed")
        if seed is None or seed != wet.get("job", {}).get("seed"):
            flags.append("paired_seed_missing_or_mismatched")
            continue
        if seed in seeds:
            flags.append("duplicate_paired_seed")
            continue
        a, b = _finite(base.get("qf")), _finite(wet.get("qf"))
        if a is None or b is None or a <= 0 or b < 0:
            flags.append("invalid_paired_qf")
            continue
        seeds.add(seed)
        paired.append((repeat, a, b, ee(base), ee(wet)))
    first_eligible_prefix = None
    for count in range(minimum, len(paired) + 1):
        prefix = paired[:count]
        means = [statistics.fmean(values) if values else None for values in
                 ([row[3] for row in prefix if row[3] is not None], [row[4] for row in prefix if row[4] is not None])]
        if any(value is not None and lo <= value <= hi for value in means):
            first_eligible_prefix = count
            break
    required = current_eligible or first_eligible_prefix is not None
    n = len(paired)
    factor = student_t_95_factor(n - 1) if n > 1 else None
    differences = [row[2] - row[1] for row in paired]
    difference = statistics.fmean(differences) if differences else None
    difference_sem = _sem(differences)
    base_mean = statistics.fmean(row[1] for row in paired) if paired else None
    contrast = {}
    for name, coefficient in (("lower_material_boundary", 1 - delta), ("upper_material_boundary", 1 + delta)):
        values = [row[2] - coefficient * row[1] for row in paired]
        mean, sem = (statistics.fmean(values) if values else None), _sem(values)
        half_width = factor * sem if factor is not None and sem is not None else None
        contrast[name] = {"mean": mean, "sem": sem,
                          "ci_lower": mean - half_width if half_width is not None else None,
                          "ci_upper": mean + half_width if half_width is not None else None}
    classification = "inconclusive"
    if n >= minimum:
        lower, upper = contrast["lower_material_boundary"], contrast["upper_material_boundary"]
        if lower["ci_lower"] > 0 and upper["ci_upper"] < 0:
            classification = "equivalent_within_material_boundary"
        elif upper["ci_lower"] > 0:
            classification = "material_increase"
        elif lower["ci_upper"] < 0:
            classification = "material_decrease"
    else:
        flags.append("insufficient_matching_seed_pairs")
    resolved = classification != "inconclusive"
    if not required:
        classification = "outside_requested_domain"
    return {"required": required, "resolved": resolved, "classification": classification,
            "n_pairs": n, "minimum_pairs": minimum, "paired_repeat_indices": [row[0] for row in paired],
            "current_mean_eee_kev": current_means, "current_domain_eligible": current_eligible,
            "first_eligible_prefix_pairs": first_eligible_prefix, "sticky_domain_eligible": first_eligible_prefix is not None,
            "requested_eee_interval_kev": [lo, hi], "material_relative_change": delta,
            "qf_difference": difference, "qf_difference_sem": difference_sem,
            "paired_baseline_qf_mean": base_mean,
            "qf_relative_change": difference / base_mean if difference is not None and base_mean else None,
            "confidence_level": 0.95, "confidence_t_factor": factor, "contrasts": contrast,
            "nominal_fixed_interval": True, "sequential_coverage_guarantee": False,
            "simultaneous_coverage_guarantee": False,
            "uncertainty_method": "Matching-seed paired linear contrasts with fixed 95% Student-t intervals; repeated adaptive looks are nominal only",
            "flags": sorted(set(flags))}


def _repeat_plan(production_batches, cfg):
    sim = cfg["simulation"]
    minimum = max(3, int(sim.get("independent_batches", 3)))
    maximum = int(sim.get("maximum_batches", 9))
    if maximum < minimum:
        raise ValueError("Maximum independent batches must permit the required initial batches")
    if maximum > 30:
        raise ValueError("Adaptive planning supports at most 30 independent batches")
    proposals, diagnostics = [], []
    for (species, energy), media in sorted(_production_groups(production_batches).items()):
        stats = _convergence(media, cfg)
        impurity = impurity_diagnostic(media, cfg)
        precision_unresolved = not all(row["converged"] for row in stats.values())
        impurity_unresolved = impurity["required"] and not impurity["resolved"]
        unresolved = precision_unresolved or impurity_unresolved
        next_repeat = None
        for repeat in range(minimum):
            if any(repeat not in media[medium] for medium in MEDIA):
                next_repeat = repeat
                break
        if next_repeat is None:
            # Finish an already-started pair even if its current unequal samples
            # happen to pass the precision thresholds independently.
            next_repeat = next((repeat for repeat in range(minimum, maximum)
                                if sum(repeat in media[medium] for medium in MEDIA) == 1), None)
        if next_repeat is None and unresolved:
            next_repeat = next((repeat for repeat in range(minimum, maximum)
                                if any(repeat not in media[medium] for medium in MEDIA)), None)
        flags = [f"{medium}:{flag}" for medium in MEDIA for flag in stats[medium]["flags"]]
        if impurity_unresolved:
            flags.append("impurity_qf_classification_unresolved")
            flags.extend("impurity:" + flag for flag in impurity["flags"])
        exhausted = unresolved and next_repeat is None
        if exhausted:
            flags.append("maximum_independent_batches_exhausted")
        diagnostics.append({"species": species, "energy_kevnr": energy,
                            "converged": not unresolved, "exhausted": exhausted,
                            "precision_converged": not precision_unresolved,
                            "next_repeat": next_repeat, "flags": flags, "media": stats,
                            "impurity": impurity})
        if next_repeat is None:
            continue
        for medium in MEDIA:
            if next_repeat in media[medium]:
                continue
            proposals.append({"stage": "production", "medium": medium,
                              "species": species, "energy_kevnr": energy,
                              "repeat": next_repeat, "n_ions": int(sim["ions_per_batch"]),
                              "displacement_energy_ev": sim["displacement_energy_ev"],
                              "exyz_energy_samples": sim["exyz_energy_samples"],
                              "planning_flags": flags})
    return proposals, diagnostics


def adaptive_repeats(production_batches, cfg) -> list[dict[str, Any]]:
    """Propose at most one new repeat index per point, paired across media.

    Production and refinement histories share convergence statistics. Missing
    initial repeats are completed first. Thereafter either medium failing
    QF/batch-SEM or event-level projected-range-SEM precision triggers both media.
    An unresolved in-domain paired impurity classification also triggers both
    media. Additional samples do not supply a sequential 95% coverage guarantee.
    Already completed jobs are never repeated. Seeds and job IDs are assigned by
    the caller so its normal provenance policy also covers these extra jobs.
    """
    return _repeat_plan(production_batches, cfg)[0]


def convergence_diagnostics(production_batches, cfg) -> list[dict[str, Any]]:
    """Retain unresolved cases even when no additional batch is allowed."""
    return _repeat_plan(production_batches, cfg)[1]
