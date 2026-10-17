#!/usr/bin/env python3
"""Read-only, deterministic review of a completed CYGNO SRIM study.

Run with Python 3.10+; standard library only. This script never changes study
files or launches simulations. JSON is written to --output, or standard output.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path


def _beta_cf(a, b, x):
    """Continued fraction for the regularized incomplete beta function."""
    tiny = 1e-300
    c = 1.0
    d = 1.0 - (a + b) * x / (a + 1.0)
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 401):
        aa = m * (b - m) * x / ((a + 2*m - 1) * (a + 2*m))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (a + b + m) * x / ((a + 2*m) * (a + 2*m + 1))
        d = 1.0 + aa * d
        c = 1.0 + aa / c
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 2e-14:
            return h
    raise ArithmeticError("Incomplete-beta continued fraction did not converge")


def _ibeta(x, a, b):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    factor = math.exp(math.lgamma(a+b) - math.lgamma(a) - math.lgamma(b)
                      + a*math.log(x) + b*math.log1p(-x))
    if x < (a+1)/(a+b+2):
        return factor * _beta_cf(a, b, x) / a
    return 1.0 - factor * _beta_cf(b, a, 1-x) / b


def _t_two_sided_p(t, dof):
    return _ibeta(dof / (dof+t*t), dof/2, .5)


def _t_critical_95(dof):
    lo, hi = 0.0, 100.0
    for _ in range(90):
        mid = (lo+hi)/2
        if _t_two_sided_p(mid, dof) > .05:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


def build_review(study_dir):
    names = ("summary.csv", "validation.json", "impurity_diagnostics.json",
             "systematics.json", "srmodule_medium_comparison.json",
             "campaign_completion.json", "archive_validation.json")
    raw = {name: (study_dir/name).read_bytes() for name in names}
    data = {name: json.loads(value) for name, value in raw.items() if name.endswith(".json")}
    summaries = list(csv.DictReader(raw["summary.csv"].decode("utf-8-sig").splitlines()))
    checks = data["validation.json"]["checks"]
    reversals = []
    for medium in sorted({r["medium"] for r in summaries}):
        for species in sorted({r["species"] for r in summaries}):
            curve = sorted((r for r in summaries if r["medium"] == medium and r["species"] == species),
                           key=lambda r: float(r["energy_kevnr"]))
            for a, b in zip(curve, curve[1:]):
                delta = float(b["eee_srim_kev_mean"]) - float(a["eee_srim_kev_mean"])
                if delta > 0:
                    continue
                sems = [float(r["eee_srim_kev_sem"]) for r in (a, b)]
                ns = [int(r["n_batches"]) for r in (a, b)]
                sem = math.hypot(*sems)
                dof = sum(s*s for s in sems)**2 / sum(s**4/(n-1) for s, n in zip(sems, ns))
                critical = _t_critical_95(dof)
                points = [{key: (int(r[key]) if key in ("n_batches", "n_ions_total") else float(r[key]))
                           for key in ("energy_kevnr", "eee_srim_kev_mean", "eee_srim_kev_sem",
                                       "qf_mean", "qf_sem", "n_batches", "n_ions_total",
                                       "primary_ionization_kev_mean", "secondary_ionization_kev_mean",
                                       "energy_closure_fraction_mean", "energy_budget_relative_residual_max_abs")}
                          for r in (a, b)]
                reversals.append({"medium": medium, "species": species, "points": points,
                    "difference_eee_keV": delta, "difference_sem_keV": sem,
                    "difference_over_sem": delta/sem, "welch_degrees_of_freedom": dof,
                    "welch_nominal_two_sided_p": _t_two_sided_p(delta/sem, dof),
                    "welch_nominal_95_interval_keV": [delta-critical*sem, delta+critical*sem],
                    "both_below_original_requested_electronic_minimum": all(p["eee_srim_kev_mean"] < .25 for p in points),
                    "statistically_significant_at_nominal_5_percent": _t_two_sided_p(delta/sem, dof) < .05,
                    "interpretation": "Strict raw monotonicity failure is retained; this difference is consistent with finite Monte Carlo statistics."})
    impurities = data["impurity_diagnostics.json"]["rows"]
    required = [x for x in impurities if x["required"]]
    impurity_species = []
    for species in sorted({r["species"] for r in impurities}):
        rows = [r for r in required if r["species"] == species]
        impurity_species.append({"species": species, "n_required": len(rows),
            "classifications": dict(Counter(r["classification"] for r in rows)),
            "sampled_energies_keVnr": [min(r["energy_kevnr"] for r in rows), max(r["energy_kevnr"] for r in rows)],
            "qf_relative_change_range": [min(r["qf_relative_change"] for r in rows), max(r["qf_relative_change"] for r in rows)]})
    systematics = data["systematics.json"]["rows"]
    thresholds = [r for r in systematics if r["comparison"] == "threshold_vs_nominal"]
    sampling = []
    for r in systematics:
        if r["comparison"] != "track_refinement":
            continue
        row = dict(r)
        reference = next(x for x in systematics if x["comparison"] == "group" and
                         x["stage"] == "track_sampling" and x["species"] == r["species"] and
                         x["energy_kevnr"] == r["energy_kevnr"] and x["exyz_energy_samples"] == r["coarse_energy_samples"])
        row["paired_qf_relative_change"] = r["paired_qf_difference"] / reference["qf_mean"]
        row["paired_qf_difference_over_sem"] = (r["paired_qf_difference"] / r["paired_qf_difference_sem"]
            if r["paired_qf_difference_sem"] else None)
        if row["species"] == "F" and row["energy_kevnr"] == 100:
            row["additional_interpretation"] = "Nonzero QF shift is a numerical/sampling sensitivity to retain; nominal paired SEM alone does not characterize this effect. No causal mechanism established."
        sampling.append(row)
    budget_rows = [r for r in summaries if r["energy_budget_valid"] == "False"]
    return {
        "schema": "CYGNO-SRIM-final-review-v1",
        "provenance": {"study_dir": str(study_dir.resolve()),
            "input_sha256": {name: hashlib.sha256(value).hexdigest() for name, value in raw.items()},
            "replay": "python3 final_review.py --study-dir /path/to/study --output final_review.json"},
        "campaign_completion_status": data["campaign_completion.json"]["status"],
        "campaign_completed_at": data["campaign_completion.json"]["completed_at"],
        "original_validation_status_retained": data["validation.json"]["status"],
        "original_failing_and_limited_checks": [r for r in checks if not r["passed"]],
        "monotonicity_review": {"reversals": reversals,
            "formulas": {"difference": "Eelect(high initial energy)-Eelect(low initial energy)",
                "SEM": "sqrt(SEM_high^2+SEM_low^2), treating distinct initial-energy batches as independent",
                "Welch_df": "(s1^2+s2^2)^2/[s1^4/(n1-1)+s2^4/(n2-1)], s=SEM, n=number of batches",
                "nominal_interval": "difference +/- t_0.975,Welch_df * SEM"},
            "caveat": "Post-hoc local diagnostic, not a simultaneous/sequential test or a detector calibration; raw means and original fatal flag remain unchanged."},
        "impurity_review": {"classification_counts": dict(Counter(r["classification"] for r in impurities)),
            "n_required": len(required), "n_resolved": sum(r["resolved"] for r in required),
            "material_relative_change_boundary": .02, "species_summary": impurity_species,
            "confidence_note": data["impurity_diagnostics.json"]["confidence_note"],
            "interpretation": "At sampled points in the original requested electronic-energy domain, all full-cascade paired QF contrasts resolve the nominal +/-2% criterion. This does not establish equivalence at all energies or equivalence of physical charge or all range distributions.",
            "srmodule_species_summary": data["srmodule_medium_comparison.json"]["species_summary"]},
        "threshold_review": {"nominal_displacement_energy_eV": 21.0,
            "batches_per_group": 3, "ions_per_batch": 100,
            "contrasts": thresholds,
            "interpretation": "Threshold shifts are model sensitivity; they are not included in QF_sem. Difference SEM uses independent batch-mean quadrature, as in the source analysis. No automatic correction is applied."},
        "track_sampling_review": {"comparisons": sampling,
            "criterion": "abs(mean paired fractional path change)+2*SEM <= 0.03 with a distinct smaller EXYZ increment",
            "interpretation": "All 1keV comparisons hit the same 1eV floor and provide no refinement test. At 100keV C and O do not meet the 3% diagnostic; all five pass at 1000keV. Sampled primary polylines remain lower bounds; actual primary and full-cascade path lengths are unknown."},
        "energy_accounting_review": {"n_summary_points_flagged": len(budget_rows),
            "flagged_points_in_original_requested_electronic_domain": sum(float(r["eee_srim_kev_mean"]) >= .25 for r in budget_rows),
            "max_abs_batch_closure_residual_fraction": max(float(r["energy_budget_relative_residual_max_abs"]) for r in summaries),
            "interpretation": "Original requested domain is Eelect>=0.25keV. The new CYGNO-MC export instead starts at initial energy 0.25keV per ion, so retained low-energy closure warnings remain relevant to its low-end raw points. Do not renormalize channels or erase flags."},
        "raw_archive_validation": {key: value for key, value in data["archive_validation.json"].items() if key != "rows"},
        "recommendation": "Proceed with documented export and existing-data analysis; do not launch an additional broad scan automatically. Keep the strict monotonicity failure and all modeling/track/calibration limitations visible. Targeted reruns require a specific unresolved scientific need.",
        "physical_charge_qf_calibrated": False,
        "simulations_launched_by_this_review": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(build_review(args.study_dir), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
