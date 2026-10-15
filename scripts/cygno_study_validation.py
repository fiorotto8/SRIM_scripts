"""Read-only systematic studies and raw-archive checks for native CYGNO runs."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import statistics
import tarfile
from typing import Any, Mapping, Sequence


def _summary(values):
    return (statistics.fmean(values), statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else None) if values else (None, None)


def _finite(value):
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def _check(name, passed, detail, severity="failure"):
    return {"check": name, "passed": bool(passed), "severity": "pass" if passed else severity, "detail": detail}


def compare_srmodule_media(
    rows: Sequence[Mapping[str, Any]],
    energy_min_kevnr: float = 0.1,
    energy_max_kevnr: float = 3000,
    material_relative_change: float = 0.02,
) -> dict[str, Any]:
    """Compare exact native table points, with no interpolation or TRIM inference.

    Relative change is contaminated/baseline - 1 at the same incident energy.
    These are deterministic SRModule predictions with no assigned statistical
    or model uncertainty. A passing 2% screen concerns only the listed stopping
    and range quantities, never full-cascade QF or physical charge response.
    """
    if not all(_finite(value) for value in (energy_min_kevnr, energy_max_kevnr, material_relative_change)):
        raise ValueError("SRModule comparison bounds and criterion must be finite")
    if not 0 < energy_min_kevnr <= energy_max_kevnr or material_relative_change < 0:
        raise ValueError("Invalid SRModule comparison energy bounds or criterion")
    quantities = ("electronic_stopping_mev_mg_cm2", "nuclear_stopping_mev_mg_cm2",
                  "electronic_stopping_kev_mm", "nuclear_stopping_kev_mm",
                  "projected_range_mm", "longitudinal_straggling_mm", "lateral_straggling_mm")
    required = (quantities[0], quantities[1], *quantities[4:])
    indexed = {}
    keys = set()
    for row in rows:
        medium = row.get("medium")
        if medium not in ("baseline", "contaminated") or not _finite(row.get("energy_kevnr")):
            continue
        energy = float(row["energy_kevnr"])
        if not energy_min_kevnr <= energy <= energy_max_kevnr:
            continue
        species = str(row["species"])
        key = (species, energy, medium)
        if key in indexed:
            raise ValueError(f"Duplicate native SRModule comparison point {key}")
        indexed[key] = row
        keys.add((species, energy))
    comparisons = []
    for species, energy in sorted(keys):
        base = indexed.get((species, energy, "baseline"))
        wet = indexed.get((species, energy, "contaminated"))
        comparison = {"species": species, "energy_kevnr": energy,
            "baseline_present": base is not None, "contaminated_present": wet is not None,
            "paired_native_point": base is not None and wet is not None,
            "deterministic_model_uncertainty": None,
            "method": "Exact native SRModule table points; deterministic stopping/range comparison, not TRIM/QF"}
        for quantity in quantities:
            a, b = base.get(quantity) if base else None, wet.get(quantity) if wet else None
            comparison[quantity + "_relative_change"] = (
                float(b) / float(a) - 1 if _finite(a) and _finite(b) and float(a) != 0 else None)
        comparisons.append(comparison)
    summaries, checks = [], []
    for species in sorted({row["species"] for row in comparisons}):
        species_rows = [row for row in comparisons if row["species"] == species]
        paired = sum(row["paired_native_point"] for row in species_rows)
        summary = {"species": species, "native_points_in_union": len(species_rows),
            "paired_native_points": paired,
            "baseline_native_points": sum(row["baseline_present"] for row in species_rows),
            "contaminated_native_points": sum(row["contaminated_present"] for row in species_rows),
            "complete_native_point_pairing": paired == len(species_rows),
            "material_relative_change_criterion": material_relative_change,
            "deterministic_model_uncertainty": None,
            "full_cascade_qf_equivalence": None, "physical_charge_equivalence": None}
        all_changes = []
        complete_required = paired == len(species_rows)
        for quantity in quantities:
            changes = [row[quantity + "_relative_change"] for row in species_rows
                       if row[quantity + "_relative_change"] is not None]
            summary[quantity + "_valid_pairs"] = len(changes)
            summary[quantity + "_max_abs_relative_change"] = max(map(abs, changes)) if changes else None
            all_changes.extend(changes)
            if quantity in required and len(changes) != len(species_rows):
                complete_required = False
        within = max(map(abs, all_changes)) <= material_relative_change if all_changes else None
        summary["all_available_changes_within_criterion"] = within
        summary["required_quantities_complete"] = complete_required
        summary["srmodule_screen_passed"] = bool(complete_required and within is True)
        summaries.append(summary)
        checks.append(_check("srmodule_impurity_screen_" + species, summary["srmodule_screen_passed"],
            f"{paired}/{len(species_rows)} native energy pairs; required quantities complete={complete_required}; "
            f"all available changes within {material_relative_change:.1%}={within}; deterministic SRModule only, no TRIM/QF or charge-equivalence inference",
            "warning"))
    if not comparisons:
        checks.append(_check("srmodule_impurity_native_pairs", False, "No native SRModule points in requested energy interval"))
    return {"rows": comparisons, "species_summary": summaries, "checks": checks,
        "definition": "Relative change = contaminated / baseline - 1 at exact matching native species/energy points; no interpolation; uncertainties unassigned; SRModule stopping/ranges only, not full-cascade QF or detector charge equivalence"}


def analyse_systematics(batches: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize direct batches without inventing missing samples or errors.

    Threshold differences use independent combined batch SEMs. Trajectory
    refinement uses paired batch fractional changes only for matching seeds.
    This can validate convergence of the sampled primary polyline; it never
    establishes an exact primary trajectory or a total recoil-cascade length.
    """
    sim, system = cfg["simulation"], cfg["systematics"]
    expected = int(system["independent_batches"])
    expected_n = int(system["ions_per_batch"])
    nominal = float(sim["displacement_energy_ev"])
    base_samples = int(sim["exyz_energy_samples"])
    factors = sorted(set(int(value) for value in system["exyz_refinement_factors"]))
    groups = defaultdict(list)
    for batch in batches:
        job = batch.get("job", {})
        if job.get("stage") not in ("systematics", "track_sampling") or job.get("medium") != "baseline":
            continue
        try:
            key = (job["stage"], str(job["species"]), float(job["energy_kevnr"]),
                   float(job["displacement_energy_ev"]), int(job["exyz_energy_samples"]))
        except (KeyError, TypeError, ValueError):
            continue
        groups[key].append(batch)
    rows, checks, indexed = [], [], {}
    for species in cfg["study"]["species"]:
        for energy in system["energies_kevnr"]:
            desired = [("systematics", float(threshold), base_samples) for threshold in system["displacement_energies_ev"]]
            desired += [("track_sampling", nominal, base_samples * factor) for factor in factors]
            for stage, threshold, samples in desired:
                key = (stage, species, float(energy), threshold, samples)
                values = groups.get(key, [])
                repeats = [batch["job"].get("repeat") for batch in values]
                seeds = [batch["job"].get("seed") for batch in values]
                valid = [batch for batch in values if _finite(batch.get("qf")) and 0 <= float(batch["qf"]) <= 1.001
                         and int(batch["job"].get("n_ions", 0)) == expected_n
                         and batch.get("effective_settings", {}).get("cascade_type") == 5
                         and not any(batch.get(field) for field in ("clipped", "backscattered", "transmitted"))]
                complete = (len(values) == expected and len(valid) == expected
                            and len(set(repeats)) == expected and None not in repeats
                            and len(set(seeds)) == expected and None not in seeds)
                qf, qf_sem = _summary([float(batch["qf"]) for batch in valid])
                lengths = [float(batch["paths"]["sampled_primary_polyline_length_mean_mm"]) for batch in valid
                           if batch.get("paths") and _finite(batch["paths"].get("sampled_primary_polyline_length_mean_mm"))]
                path_mean, path_sem = _summary(lengths)
                steps = sorted(set(float(batch["exyz_step_ev"]) for batch in valid if _finite(batch.get("exyz_step_ev"))))
                row = {"comparison": "group", "stage": stage, "species": species, "energy_kevnr": float(energy),
                    "displacement_energy_ev": threshold, "exyz_energy_samples": samples,
                    "exyz_steps_ev": steps, "expected_batches": expected, "n_batches": len(values),
                    "n_valid_batches": len(valid), "n_valid_path_batches": len(lengths),
                    "complete": complete, "status": "complete" if complete else "pending_or_invalid",
                    "qf_mean": qf, "qf_sem": qf_sem, "sampled_primary_path_mm_mean": path_mean,
                    "sampled_primary_path_mm_sem": path_sem,
                    "actual_primary_trajectory_length_mm": None, "total_cascade_trajectory_length_mm": None}
                rows.append(row)
                indexed[key] = (row, valid)
                label = f"{stage}_{species}_{energy:g}_{threshold:g}_{samples}"
                checks.append(_check("systematics_batches_" + label, complete,
                    f"{len(valid)}/{expected} valid full-cascade batches; unique repeats/seeds and N={expected_n} required"))
            reference_key = ("track_sampling", species, float(energy), nominal, base_samples * factors[0])
            reference, reference_batches = indexed[reference_key]
            for threshold in system["displacement_energies_ev"]:
                row, _ = indexed[("systematics", species, float(energy), float(threshold), base_samples)]
                difference = row["qf_mean"] - reference["qf_mean"] if row["qf_mean"] is not None and reference["qf_mean"] is not None else None
                sem = math.hypot(row["qf_sem"], reference["qf_sem"]) if row["qf_sem"] is not None and reference["qf_sem"] is not None else None
                rows.append({"comparison": "threshold_vs_nominal", "species": species, "energy_kevnr": float(energy),
                    "displacement_energy_ev": float(threshold), "reference_displacement_energy_ev": nominal,
                    "qf_difference": difference, "qf_difference_sem": sem,
                    "qf_relative_change": difference / reference["qf_mean"] if difference is not None and reference["qf_mean"] else None,
                    "complete": row["complete"] and reference["complete"],
                    "uncertainty_method": "Independent batch means; difference SEM is quadrature of both batch SEMs"})
            for factor in factors[1:]:
                fine, fine_batches = indexed[("track_sampling", species, float(energy), nominal, base_samples * factor)]
                coarse_by_repeat = {b["job"]["repeat"]: b for b in reference_batches}
                fine_by_repeat = {b["job"]["repeat"]: b for b in fine_batches}
                changes, qf_changes = [], []
                for repeat in sorted(set(coarse_by_repeat) & set(fine_by_repeat)):
                    coarse_batch, fine_batch = coarse_by_repeat[repeat], fine_by_repeat[repeat]
                    if coarse_batch["job"].get("seed") != fine_batch["job"].get("seed"):
                        continue
                    a = (coarse_batch.get("paths") or {}).get("sampled_primary_polyline_length_mean_mm")
                    b = (fine_batch.get("paths") or {}).get("sampled_primary_polyline_length_mean_mm")
                    if _finite(a) and _finite(b) and float(a) > 0:
                        changes.append(float(b) / float(a) - 1)
                        qf_changes.append(float(fine_batch["qf"]) - float(coarse_batch["qf"]))
                change, sem = _summary(changes)
                qf_change, qf_sem = _summary(qf_changes)
                same_steps = reference["exyz_steps_ev"] == fine["exyz_steps_ev"] and bool(reference["exyz_steps_ev"])
                actual_refinement = (len(reference["exyz_steps_ev"]) == len(fine["exyz_steps_ev"]) == 1
                                     and fine["exyz_steps_ev"][0] < reference["exyz_steps_ev"][0])
                complete = reference["complete"] and fine["complete"] and len(changes) == expected
                tolerance = float(cfg["validation"].get("maximum_range_relative_sem", 0.03))
                # A two-SEM diagnostic, not a claimed exact confidence interval.
                upper = abs(change) + 2 * sem if change is not None and sem is not None else None
                resolved = complete and actual_refinement and upper is not None and upper <= tolerance
                rows.append({"comparison": "track_refinement", "species": species, "energy_kevnr": float(energy),
                    "coarse_energy_samples": reference["exyz_energy_samples"], "fine_energy_samples": fine["exyz_energy_samples"],
                    "coarse_exyz_steps_ev": reference["exyz_steps_ev"], "fine_exyz_steps_ev": fine["exyz_steps_ev"],
                    "n_paired_batches": len(changes), "complete": complete, "same_actual_exyz_increment": same_steps,
                    "actual_sampling_refinement": actual_refinement, "paired_relative_path_change_mean": change,
                    "paired_relative_path_change_sem": sem, "path_change_absolute_plus_two_sem": upper,
                    "sampled_polyline_convergence_resolved": bool(resolved),
                    "paired_qf_difference": qf_change, "paired_qf_difference_sem": qf_sem,
                    "actual_primary_trajectory_length_mm": None, "total_cascade_trajectory_length_mm": None,
                    "interpretation": "Sampled primary polyline only; equal increments at the 1 eV floor provide no refinement test"})
                name = f"track_sampling_{species}_{energy:g}_{factor}"
                checks.append(_check(name + "_pairs", complete, f"{len(changes)}/{expected} valid paired batches with identical seeds"))
                checks.append(_check(name + "_distinct_increment", actual_refinement,
                    f"coarse {reference['exyz_steps_ev']} eV; fine {fine['exyz_steps_ev']} eV; equal increments do not test refinement", "limitation" if same_steps else "failure"))
                checks.append(_check(name + "_polyline_convergence", resolved,
                    f"Absolute paired path change plus two SEM={upper}; criterion={tolerance}; exact/cascade lengths unknown",
                    "limitation" if same_steps else "warning" if complete else "failure"))
    return {"rows": rows, "checks": checks}


def verify_archives(study_dir: Path, batches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Hash regular archive members without extracting any content to disk."""
    root = study_dir.resolve()
    rows = []
    for batch in batches:
        job_id = str(batch.get("job", {}).get("job_id", "unknown"))
        row = {"job_id": job_id, "raw_archive": batch.get("raw_archive"), "passed": False, "detail": ""}
        try:
            relative = Path(str(batch["raw_archive"]))
            archive = (root / relative).resolve()
            if relative.is_absolute() or not archive.is_relative_to(root):
                raise ValueError("Archive path leaves study directory")
            expected = dict(batch["raw_sha256"])
            if not expected:
                raise ValueError("Expected archive hash map is empty")
            actual, manifest = {}, None
            with tarfile.open(archive, "r:*") as handle:
                seen = set()
                for member in handle.getmembers():
                    name = member.name
                    posix = PurePosixPath(name)
                    if posix.is_absolute() or ".." in posix.parts or "\\" in name or name in seen:
                        raise ValueError(f"Unsafe or duplicate archive member {name!r}")
                    seen.add(name)
                    if not member.isfile():
                        raise ValueError(f"Archive member is not a regular file: {name!r}")
                    stream = handle.extractfile(member)
                    if stream is None:
                        raise ValueError(f"Unreadable archive member {name!r}")
                    if name == "manifest.json":
                        manifest = json.load(stream)
                    else:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                        actual[name] = digest.hexdigest()
            if actual != expected:
                missing = sorted(set(expected) - set(actual))
                extra = sorted(set(actual) - set(expected))
                changed = sorted(name for name in set(expected) & set(actual) if expected[name] != actual[name])
                raise ValueError(f"Archive hash mismatch: missing={missing}; extra={extra}; changed={changed}")
            if not isinstance(manifest, dict) or manifest.get("sha256") != expected:
                raise ValueError("Archive manifest sha256 map differs from result hash map")
            if str(manifest.get("job", {}).get("job_id", "unknown")) != job_id:
                raise ValueError("Archive manifest job differs from batch job")
            row.update(passed=True, detail=f"{len(actual)} regular files match result and manifest SHA256 maps")
        except (OSError, KeyError, ValueError, TypeError, tarfile.TarError) as error:
            row["detail"] = f"{type(error).__name__}: {error}"
        rows.append(row)
    verified = sum(row["passed"] for row in rows)
    passed = bool(rows) and verified == len(rows)
    return {"rows": rows, "checks": [_check("raw_archive_sha256", passed,
            f"{verified}/{len(rows)} archives verified by content without extraction")],
        "verified_archives": verified, "expected_archives": len(rows), "passed": passed}
