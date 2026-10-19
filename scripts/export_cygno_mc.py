#!/usr/bin/env python3
"""Read-only SRIM snapshot and CYGNO-MC interchange export; never launches SRIM.

Requires NumPy, SciPy and Matplotlib in native Windows Python.
Replay from the exported snapshot, rather than a changing live study directory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import tarfile

import numpy as np
import scipy
from scipy.interpolate import BSpline
from scipy.optimize import minimize

SPECIES = {"H": ("H1", 1, 1), "He": ("He4", 2, 4), "C": ("C12", 6, 12),
           "O": ("O16", 8, 16), "F": ("F19", 9, 19)}
FIELDS = ["species", "Z", "A", "energy_keV", "QF", "QF_sem", "n_ions", "run_id", "is_direct"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def write_csv(path, rows, fields=None):
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows({k: int(v) if isinstance(v, bool) else v for k, v in row.items()} for row in rows)


def validate_config(config):
    target = config["target"]
    expected = {"He": 3 / 13, "C": 2 / 13, "F": 8 / 13}
    if target["molar_fractions"] != {"He": .6, "CF4": .4}:
        raise ValueError("Expected He/CF4=60/40 molar")
    for key, value in expected.items():
        if not math.isclose(target["SRIM_element_atomic_fractions"][key], value, rel_tol=1e-12):
            raise ValueError("Incorrect elemental atomic fractions")
    if not math.isclose(target["pressure_mbar"], 900.) or not math.isclose(target["temperature_K"], 293.15):
        raise ValueError("Expected900mbar and293.15K")
    if not math.isclose(target["density_g_cm3"], .00138850, rel_tol=4e-6):
        raise ValueError("Density disagrees with requested rounded0.00138850g/cm3")
    primaries = config["primaries"]
    if (primaries["energy_min_keV"], primaries["energy_max_keV"]) != (.25, 1000):
        raise ValueError("Expected0.25–1000keV per ion")
    if {(x["species"], x["Z"], x["A"]) for x in primaries["ions"]} != set(SPECIES.values()):
        raise ValueError("Five requested isotope identities must match")


def capture(study, config_path, snapshot):
    """Freeze only atomically committed batch records, never open worker files."""
    config_payload = config_path.read_bytes()
    config = json.loads(config_payload)
    validate_config(config)
    metadata_path = study / "metadata.json"
    metadata_payload = metadata_path.read_bytes()
    metadata = json.loads(metadata_payload)
    gas = next(x for x in metadata["gas_conditions"] if x["medium"] == "baseline")
    if gas["molecular_fractions"] != {"He": .6, "CF4": .4}:
        raise ValueError("Source baseline gas is not the requested dry composition")
    for k, v in config["target"]["SRIM_element_atomic_fractions"].items():
        if not math.isclose(gas["atomic_fractions"][k], v, rel_tol=1e-12):
            raise ValueError("Source target atomic fractions differ")
    sim = metadata["configuration"]["simulation"]
    records, exclusions, seen = [], defaultdict(int), set()
    # List once: files committed later belong to a future export, not this snapshot.
    paths = sorted((study / "batches").glob("*.json"))
    for path in paths:
        payload = path.read_bytes()
        batch = json.loads(payload)
        job = batch["job"]
        if job["medium"] != "baseline":
            exclusions["other_medium"] += 1
            continue
        if job["stage"] not in ("production", "refinement", "track_sampling", "low_energy_extension"):
            exclusions["pilot_or_threshold_systematic"] += 1
            continue
        if job["displacement_energy_ev"] != 21 or job["exyz_energy_samples"] != 1000:
            exclusions["non_nominal_cutoff_or_correlated_fine_sampling"] += 1
            continue
        settings = batch["effective_settings"]
        expected_z = SPECIES[job["species"]][1]
        if settings["ion_z"] != expected_z or not math.isclose(settings["ion_mass_u"], metadata["isotope_projectile_masses_u"][job["species"]], abs_tol=5.1e-5):
            raise ValueError("Effective projectile isotope differs: " + path.name)
        if settings["cascade_type"] != 5 or settings["n_ions_calculated"] != job["n_ions"]:
            raise ValueError("Incomplete or non-full-cascade batch: " + path.name)
        if batch["endpoints"]["n_records"] != job["n_ions"]:
            raise ValueError("Incomplete endpoint count: " + path.name)
        if any(batch[x] for x in ("clipped", "backscattered", "transmitted")) or settings["target_escape_count"]:
            exclusions["escaping_or_truncated_batch"] += 1
            continue
        if not math.isclose(settings["density_g_cm3"], gas["density_g_cm3"], rel_tol=2e-5):
            raise ValueError("Effective gas density mismatch: " + path.name)
        if set(settings["target_element_order_z"]) != {2, 6, 9}:
            raise ValueError("Effective baseline target elements differ")
        for setting in settings["target_energy_settings"].values():
            if (setting["displacement_energy_ev"] != 21 or setting["lattice_binding_energy_ev"] != 0
                    or setting["surface_binding_energy_ev"] != 0):
                raise ValueError("Unexpected effective cutoff/binding: " + path.name)
        # Same-seed coarse/fine tracks are correlated, not independent extra histories.
        identity = (job["species"], job["energy_kevnr"], job["seed"])
        if identity in seen:
            raise ValueError("Duplicate seed at same species/energy")
        seen.add(identity)
        budget = batch["budget"]
        primary, recoil = (budget[k] for k in ("primary_electronic_energy_kev", "secondary_electronic_energy_kev"))
        profile = budget["ioniz_profile"]
        # Reproduce the existing rectangular-bin, already-per-ion integral.
        for col, value in ((1, primary), (2, recoil)):
            integrated = math.fsum(row[col] for row in profile["rows"]) * profile["bin_width_angstrom"] / 1000
            if not math.isclose(integrated, value, rel_tol=1e-11, abs_tol=1e-14):
                raise ValueError("Stored IONIZ integral does not reproduce " + path.name)
        if not math.isclose((primary + recoil) / job["energy_kevnr"], batch["qf"], rel_tol=1e-11):
            raise ValueError("Inclusive QF bookkeeping mismatch")
        source_archive = study / batch["raw_archive"]
        if not source_archive.is_file():
            raise ValueError("Missing raw archive: " + str(source_archive))
        records.append({"job": job, "source_json": "batches/" + path.name,
                        "source_json_sha256": hashlib.sha256(payload).hexdigest(),
                        "source_revision": batch.get("source_revision"),
                        "raw_archive": str(batch["raw_archive"]).replace("\\", "/"), "raw_sha256": batch["raw_sha256"],
                        "primary_keV": primary, "recoil_keV": recoil,
                        "phonon_keV": budget.get("phonon_energy_kev"),
                        "binding_keV": budget.get("binding_energy_sink_kev"),
                        "closure_fraction": budget.get("exclusive_energy_budget_with_binding_residual_fraction"),
                        "effective_settings": settings, "endpoint_count": batch["endpoints"]["n_records"],
                        "target_width_angstrom": batch["target_width_angstrom"],
                        "exyz_step_ev": batch["exyz_step_ev"]})
    if not records:
        raise ValueError("No qualified nominal full-cascade results")
    snapshot.mkdir(parents=True)
    (snapshot / "geant4_config.json").write_bytes(config_payload)
    (snapshot / "source_metadata.json").write_bytes(metadata_payload)
    examples = snapshot / "input_examples"
    examples.mkdir()
    input_checks = []
    for symbol in SPECIES:
        example = next((b for b in records if b["job"]["species"] == symbol), None)
        if example is None:
            continue
        with tarfile.open(study / example["raw_archive"], "r:gz") as archive:
            raw_input = archive.extractfile("TRIM.IN").read()
        digest = hashlib.sha256(raw_input).hexdigest()
        if digest != example["raw_sha256"]["TRIM.IN"]:
            raise ValueError("Example TRIM.IN hash mismatch")
        lines = raw_input.decode("ascii").splitlines()
        phase_line = next(i for i, line in enumerate(lines) if "Target layer phases" in line)
        if lines[phase_line + 1].strip() != "1":
            raise ValueError("Example input does not request gas phase")
        (examples / f"{SPECIES[symbol][0]}_TRIM.IN").write_bytes(raw_input)
        input_checks.append({"species": SPECIES[symbol][0], "batch_id": example["job"]["job_id"],
                             "sha256": digest, "requested_gas_phase": 1})
    write_json(snapshot / "input_example_validation.json", input_checks)
    for name in ("source_installation.json",):
        p = study / "provenance" / name
        if p.exists():
            shutil.copy2(p, snapshot / name)
    shutil.copy2(study / "srmodule.csv", snapshot / "srmodule.csv")
    with (snapshot / "batches.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in records:
            handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    write_json(snapshot / "capture.json", {"captured_at": datetime.now(timezone.utc).isoformat(),
               "source_study": str(study.resolve()), "source_config_sha256": hashlib.sha256(config_payload).hexdigest(),
               "listed_source_batch_count": len(paths), "selected_batch_count": len(records),
               "exclusions": dict(exclusions), "source_simulation_settings": sim,
               "raw_archive_integrity_scope": "Original member hashes retained; raw archives exist; one TRIM.IN per isotope hash/gas phase verified. This exporter does not rehash all compressed archive contents."})
    write_json(snapshot / "sha256.json", {p.relative_to(snapshot).as_posix(): sha(p) for p in sorted(snapshot.rglob("*")) if p.is_file()})


def load_snapshot(snapshot):
    for name, digest in json.loads((snapshot / "sha256.json").read_text()).items():
        if sha(snapshot / name) != digest:
            raise ValueError("Frozen input hash mismatch: " + name)
    config = json.loads((snapshot / "geant4_config.json").read_text())
    validate_config(config)
    records = [json.loads(line) for line in (snapshot / "batches.jsonl").read_text().splitlines()]
    return config, json.loads((snapshot / "source_metadata.json").read_text()), records


def aggregate(records, source_id):
    groups = defaultdict(list)
    for row in records:
        j = row["job"]
        groups[(j["species"], j["energy_kevnr"])].append(row)
    points = []
    for (symbol, energy), batches in sorted(groups.items()):
        species, z, mass = SPECIES[symbol]
        # Production batches all have the same event count; reject ambiguous weighting.
        if len({b["job"]["n_ions"] for b in batches}) != 1:
            raise ValueError("Unequal batch sizes need a separate uncertainty estimator")
        channels = np.array([[b["primary_keV"], b["recoil_keV"]] for b in batches])
        total = channels.sum(axis=1)
        mean = channels.mean(axis=0)
        covariance = np.cov(channels.T, ddof=1) / len(batches) if len(batches) > 1 else None
        sem = statistics.stdev(total) / math.sqrt(len(total)) / energy if len(total) > 1 else None
        closures = [b["closure_fraction"] for b in batches if b["closure_fraction"] is not None]
        point = {"species": species, "Z": z, "A": mass, "energy_keV": energy,
                 "QF": float(total.mean() / energy), "QF_sem": sem,
                 "n_ions": sum(b["job"]["n_ions"] for b in batches),
                 "run_id": f"{source_id}:baseline:{species}:{energy:.12g}", "is_direct": True,
                 "primary_electronic_keV": float(mean[0]), "recoil_electronic_keV": float(mean[1]),
                 "total_electronic_keV": float(total.mean()), "remaining_energy_keV": float(energy - total.mean()),
                 "primary_electronic_keV_sem": math.sqrt(covariance[0, 0]) if covariance is not None else None,
                 "recoil_electronic_keV_sem": math.sqrt(covariance[1, 1]) if covariance is not None else None,
                 "primary_recoil_mean_covariance_keV2": float(covariance[0, 1]) if covariance is not None else None,
                 "phonon_keV": statistics.fmean(b["phonon_keV"] for b in batches) if all(b["phonon_keV"] is not None for b in batches) else None,
                 "binding_keV": statistics.fmean(b["binding_keV"] for b in batches) if all(b["binding_keV"] is not None for b in batches) else None,
                 "energy_closure_residual_fraction_mean": statistics.fmean(closures) if closures else None,
                 "energy_closure_residual_fraction_max_abs": max(map(abs, closures)) if closures else None,
                 "n_batches": len(batches), "batch_ids": [b["job"]["job_id"] for b in batches],
                 "batch_channels": channels}
        points.append(point)
    return points


def basis(knots, x, derivative=0):
    n = len(knots) - 4
    return BSpline(knots, np.eye(n), 3, extrapolate=False)(x, nu=derivative)


def fit_coefficients(design, values, errors, penalty, initial=None):
    """Convex quadratic regression with bounded nondecreasing QF coefficients."""
    n = design.shape[1]
    weights = np.median(errors) / errors
    aw, yw = design * weights[:, None], values * weights
    difference = np.diff(np.eye(n), n=2, axis=0)
    h = aw.T @ aw + penalty * difference.T @ difference
    rhs = aw.T @ yw
    scale = max(float(np.linalg.norm(h, ord=2)), 1.)
    order = np.diff(np.eye(n), axis=0)
    if initial is None:
        initial = np.linspace(max(.001, values.min()), min(.9999, values.max()), n)
    result = minimize(lambda c: (.5 * c @ h @ c - rhs @ c) / scale, initial,
                      jac=lambda c: (h @ c - rhs) / scale, method="SLSQP",
                      bounds=[(0., 1.)] * n,
                      constraints=[{"type": "ineq", "fun": lambda c: order @ c, "jac": lambda c: order}],
                      options={"maxiter": 1000, "ftol": 1e-13})
    if not result.success or np.min(order @ result.x) < -1e-8:
        raise RuntimeError("Constrained spline regression failed: " + result.message)
    return result.x


def evaluate(model, energies):
    e = np.asarray(energies, dtype=float)
    lo, hi = model["valid_energy_keV"]
    if np.any(~np.isfinite(e)) or np.any(e < lo) or np.any(e > hi):
        raise ValueError(f"Energy outside supported numerical domain[{lo},{hi}]keV per ion; extrapolation disabled")
    b = basis(model["knots_log_energy"], np.log(e))
    response_basis = b + basis(model["knots_log_energy"], np.log(e), 1)
    coefficients = np.asarray(model["coefficients"])
    covariance = np.asarray(model["coefficient_covariance"])
    q = b @ coefficients
    d = response_basis @ coefficients
    qsem = np.sqrt(np.maximum(0., np.einsum("...i,ij,...j->...", b, covariance, b)))
    dsem = np.sqrt(np.maximum(0., np.einsum("...i,ij,...j->...", response_basis, covariance, response_basis)))
    return q, qsem, d, dsem


def smooth(points, lo, hi, n_bootstrap, seed, full_support=False, n_coefficients=None, fixed_penalty=None):
    eligible = sorted([p for p in points if p["QF_sem"] is not None and p["QF_sem"] > 0], key=lambda p: p["energy_keV"])
    lower = [p for p in eligible if p["energy_keV"] < lo]
    upper = [p for p in eligible if p["energy_keV"] > hi]
    if full_support:
        fitted = [p for p in eligible if lo <= p['energy_keV'] <= hi]
        if not fitted or fitted[0]['energy_keV'] != lo or fitted[-1]['energy_keV'] != hi:
            raise ValueError('Full-support boundaries require measured endpoints with known positive SEM')
    elif not lower or not upper:
        raise ValueError("Smooth response requires measured guard points bracketing both boundaries")
    else:
        fitted = [lower[-1]] + [p for p in eligible if lo <= p["energy_keV"] <= hi] + [upper[0]]
    e = np.array([p["energy_keV"] for p in fitted])
    q = np.array([p["QF"] for p in fitted])
    errors = np.array([p["QF_sem"] for p in fitted])
    x = np.log(e)
    # Leave-one-energy-out CV; nearby independent energies remain distinct.
    candidates=(12,14,16,18,20) if full_support and n_coefficients is None else (n_coefficients or 14,)
    candidates=sorted({min(n,len(e)-3) for n in candidates})
    scores=[];designs={};knot_sets={}
    for ncoef in candidates:
        knots = np.r_[np.repeat(x[0], 4), np.linspace(x[0], x[-1], ncoef - 2)[1:-1], np.repeat(x[-1], 4)]
        design=basis(knots,x);designs[ncoef]=design;knot_sets[ncoef]=knots
        for penalty in ((fixed_penalty,) if fixed_penalty is not None else (0., .01, .1, 1., 10., 100.)):
            pulls=[]
            for i in range(len(e)):
                mask=np.arange(len(e))!=i
                c=fit_coefficients(design[mask],q[mask],errors[mask],penalty)
                pulls.append(float((design[i]@c-q[i])/errors[i]))
            score=dict(penalty=penalty,leave_one_energy_out_mean_squared_pull=float(np.mean(np.square(pulls))))
            if full_support:score['n_coefficients']=ncoef
            scores.append(score)
    chosen=min(scores,key=lambda v:v['leave_one_energy_out_mean_squared_pull'])
    penalty=chosen['penalty'];ncoef=chosen.get('n_coefficients',candidates[0])
    design=designs[ncoef];knots=knot_sets[ncoef]
    c = fit_coefficients(design, q, errors, penalty)
    rng = np.random.default_rng(seed)
    draws = []
    for _ in range(n_bootstrap):
        sample = []
        for p in fitted:
            channels = p["batch_channels"]
            n = len(channels)
            # Resample both channels jointly; correct finite-n bootstrap variance.
            resampled = channels[rng.integers(0, n, size=n)].sum(axis=1).mean() / p["energy_keV"]
            sample.append(p["QF"] + math.sqrt(n / (n - 1)) * (resampled - p["QF"]))
        draws.append(fit_coefficients(design, np.array(sample), errors, penalty, initial=c))
    draws = np.array(draws)
    pulls = (design @ c - q) / errors
    model = {"species": fitted[0]["species"], "Z": fitted[0]["Z"], "A": fitted[0]["A"],
             "valid_energy_keV": [lo, hi], "degree": 3, "coordinate": "natural logarithm of total kinetic energy in keV per ion",
             "knots_log_energy": knots.tolist(), "coefficients": c.tolist(),
             "coefficient_covariance": (np.cov(draws.T, ddof=1) if n_bootstrap > 1 else np.zeros((len(c), len(c)))).tolist(),
             "bootstrap_coefficients": draws.tolist(), "bootstrap_seed": seed,
             "bootstrap_replicates": n_bootstrap, "selected_penalty": penalty, "cross_validation": scores,
             "raw_fit_run_ids": [p["run_id"] for p in fitted], "source_ions_in_fit": sum(p["n_ions"] for p in fitted),
             "fit_energy_keV": e.tolist(), "fit_raw_QF": q.tolist(), "fit_raw_QF_sem": errors.tolist(),
             "fit_residual_pull": pulls.tolist(),
             "guard_energies_keV": [float(e[0]), float(e[-1])],
             "weighted_residual_RMS_pull": float(np.sqrt(np.mean(pulls ** 2))), "maximum_abs_residual_pull": float(max(abs(pulls))),
             "model_assumptions": "Cubic B-spline QF versus logE; coefficients constrained to[0,1] and nondecreasing. This is a smoothing/monotonic-QF assumption, not a new SRIM simulation.",
             "uncertainty": "Variance-corrected paired independent-batch bootstrap at each energy, preserving primary/recoil covariance. Conditional on chosen basis, penalty and shape constraints; no gas-transfer, SRIM-model, cutoff or detector-calibration uncertainty included.",
             "boundary_policy": f"Inclusive {lo:g}–{hi:g} keV per ion; analytic one-sided boundary values, no extrapolation or clamping."}
    if full_support:
        model.update(full_simulated_support=True, boundary_support='Measured endpoint data included; fitted means need not pass through them; no support outside bounds',
                     basis_selection='Full-support default jointly selects12/14/16/18/20 coefficients and penalty by leave-one-energy-out CV',
                     excluded_points_with_unknown_or_zero_sem=[p['run_id'] for p in points if p['run_id'] not in {v['run_id'] for v in eligible}])
    return model


def export_header(path, models):
    lines = ["// Generated CYGNO-MC response: density-transferred SRIM model, NOT exact-target direct data.",
             "// Energy is total keV per ion. Bounds are inclusive; outside throws. Monte Carlo SEM only.",
             "#pragma once", "#include <cmath>", "#include <stdexcept>", "#include <vector>",
             "namespace cygno_srim {", "struct Response { double QF, QF_sem, dRdE, dRdE_sem; };",
             "struct Model { int Z,A; double lo,hi; std::vector<double> t,c,cov; };",
             "inline const std::vector<Model>& models() { static const std::vector<Model> m = {"]
    fmt = lambda values: "{" + ",".join(format(float(v), ".17g") for v in values) + "}"
    for model in models:
        lo, hi = model['valid_energy_keV']
        lines.append("{" + f"{model['Z']},{model['A']},{lo:.17g},{hi:.17g}," + fmt(model["knots_log_energy"]) + "," + fmt(model["coefficients"]) + "," + fmt(np.ravel(model["coefficient_covariance"])) + "},")
    lines += ["}; return m; }",
              "inline double basis(const std::vector<double>& t, int i, int k, double x) {",
              "  if(k==0) return t[i]<=x && x<t[i+1] ? 1.0 : 0.0;",
              "  double a=0,b=0; if(t[i+k]>t[i]) a=(x-t[i])/(t[i+k]-t[i])*basis(t,i,k-1,x);",
              "  if(t[i+k+1]>t[i+1]) b=(t[i+k+1]-x)/(t[i+k+1]-t[i+1])*basis(t,i+1,k-1,x); return a+b; }",
              "inline Response evaluate(int Z,int A,double E) {",
              "  if(!std::isfinite(E) || E<=0) throw std::out_of_range(\"SRIM response requires finite positive total keV per ion\");",
              "  for(const auto& m:models()) if(m.Z==Z && m.A==A) {",
              "    if(E<m.lo || E>m.hi) throw std::out_of_range(\"SRIM energy outside explicit model bounds\");",
              "    const int n=static_cast<int>(m.c.size()); const double x0=std::log(E); const double x=x0==m.t.back()?std::nextafter(x0,-INFINITY):x0; std::vector<double> b(n),d(n);",
              "    double q=0,r=0,vq=0,vr=0; for(int i=0;i<n;++i) { b[i]=basis(m.t,i,3,x); double dx=0;",
              "      if(m.t[i+3]>m.t[i]) dx+=3/(m.t[i+3]-m.t[i])*basis(m.t,i,2,x);",
              "      if(m.t[i+4]>m.t[i+1]) dx-=3/(m.t[i+4]-m.t[i+1])*basis(m.t,i+1,2,x);",
              "      d[i]=b[i]+dx; q+=b[i]*m.c[i]; r+=d[i]*m.c[i]; }",
              "    for(int i=0;i<n;++i) for(int j=0;j<n;++j) { vq+=b[i]*m.cov[i*n+j]*b[j]; vr+=d[i]*m.cov[i*n+j]*d[j]; }",
              "    return {q,std::sqrt(std::fmax(0.,vq)),r,std::sqrt(std::fmax(0.,vr))}; }",
              "  throw std::invalid_argument(\"Unsupported SRIM isotope Z,A\"); }", "} // namespace cygno_srim"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def build(snapshot, out, n_bootstrap=400, grid_points=401, seed=20261016):
    config, metadata, records = load_snapshot(snapshot)
    gas = next(x for x in metadata["gas_conditions"] if x["medium"] == "baseline")
    target = config["target"]
    source_path = json.loads((snapshot / "capture.json").read_text())["source_study"]
    source_id = source_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    points = aggregate(records, source_id)
    lo, hi = config["primaries"]["energy_min_keV"], config["primaries"]["energy_max_keV"]
    full_support = json.loads((snapshot/'capture.json').read_text()).get('full_simulated_support', False)
    if full_support:
        lo = max(min(p['energy_keV'] for p in points if p['species']==sp[0]) for sp in SPECIES.values())
        hi = min(max(p['energy_keV'] for p in points if p['species']==sp[0]) for sp in SPECIES.values())
    inside = [p for p in points if lo <= p["energy_keV"] <= hi]
    required_row = lambda p: {k: p[k] for k in FIELDS}
    write_csv(out / "qf_source_direct.csv", [required_row(p) for p in inside], FIELDS)
    write_csv(out / "qf_source_with_guards.csv", [required_row(p) for p in points], FIELDS)
    write_csv(out / "qf_target_transferred.csv", [{**required_row(p), "is_direct": False} for p in inside], FIELDS)
    channel_rows = [{k: v for k, v in p.items() if k not in ("batch_channels", "batch_ids")} for p in inside]
    write_csv(out / "energy_channels_source.csv", channel_rows)
    write_csv(out / "batch_energy_channels.csv", [{"species": SPECIES[b["job"]["species"]][0], "energy_keV": b["job"]["energy_kevnr"],
              "batch_id": b["job"]["job_id"], "seed": b["job"]["seed"], "n_ions": b["job"]["n_ions"],
              "primary_electronic_keV": b["primary_keV"], "recoil_electronic_keV": b["recoil_keV"],
              "phonon_keV": b["phonon_keV"], "binding_keV": b["binding_keV"], "closure_residual_fraction": b["closure_fraction"]} for b in records])
    models, smooth_rows, response_rows, coverage = [], [], [], []
    energy_grid = np.geomspace(lo, hi, grid_points)
    for index, (symbol, (species, z, mass)) in enumerate(SPECIES.items()):
        selected = [p for p in points if p["species"] == species]
        if not selected:
            coverage.append({"species": species, "missing_entire_species": True})
            continue
        try:
            model = smooth(selected, lo, hi, n_bootstrap, seed + index, full_support=full_support)
        except ValueError as error:
            coverage.append({"species": species, "smooth_response_available": False,
                             "reason": str(error), "source_direct_min_keV": min(p["energy_keV"] for p in selected),
                             "source_direct_max_keV": max(p["energy_keV"] for p in selected),
                             "exact_target_direct_coverage": [], "missing_exact_target_direct_interval_keV": [lo, hi]})
            continue
        model["run_id"] = f"{source_id}:{'full_support_' if full_support else ''}density_transfer_spline:{species}"
        q, qsem, derivative, dsem = evaluate(model, energy_grid)
        bb = basis(model["knots_log_energy"], np.log(energy_grid))
        db = bb + basis(model["knots_log_energy"], np.log(energy_grid), 1)
        draws = np.asarray(model["bootstrap_coefficients"])
        qbounds = np.quantile(draws @ bb.T, [.025, .975], axis=0)
        dbounds = np.quantile(draws @ db.T, [.025, .975], axis=0)
        if np.min(q) < -1e-9 or np.max(q) > 1 + 1e-9 or np.min(derivative) < -1e-9:
            raise ValueError("Unphysical bounded/monotone smooth response")
        for i, energy in enumerate(energy_grid):
            row = dict(zip(FIELDS, (species, z, mass, float(energy), float(q[i]), float(qsem[i]), 0, model["run_id"], False)))
            smooth_rows.append(row)
            response_rows.append({**row, "R_keV": float(energy * q[i]), "R_keV_sem": float(energy * qsem[i]),
                "dR_dE": float(derivative[i]), "dR_dE_sem": float(dsem[i]),
                "QF_bootstrap_p025": float(qbounds[0, i]), "QF_bootstrap_p975": float(qbounds[1, i]),
                "dR_dE_bootstrap_p025": float(dbounds[0, i]), "dR_dE_bootstrap_p975": float(dbounds[1, i])})
        models.append(model)
        coverage.append({"species": species, "source_direct_min_keV": min(p["energy_keV"] for p in selected),
                         "source_direct_max_keV": max(p["energy_keV"] for p in selected),
                         "direct_source_points_in_requested_domain": sum(lo <= p["energy_keV"] <= hi for p in selected),
                         "direct_at_0_25_keV": any(p["energy_keV"] == .25 for p in selected),
                         "direct_at_1000_keV": any(p["energy_keV"] == 1000 for p in selected),
                         "direct_at_lower_bound": any(p["energy_keV"] == lo for p in selected),
                         "direct_at_upper_bound": any(p["energy_keV"] == hi for p in selected),
                         "derived_interpolation_supported_keV": [lo, hi],
                         "exact_target_direct_coverage": [], "missing_exact_target_direct_interval_keV": [lo, hi],
                         "derivative_min": float(min(derivative)), "derivative_max": float(max(derivative)),
                         "differential_exceeds_unity": bool(max(derivative) > 1),
                         "raw_energy_budget_flags": [{"energy_keV": p["energy_keV"], "max_abs_residual_fraction": p["energy_closure_residual_fraction_max_abs"]}
                            for p in selected if lo <= p["energy_keV"] <= hi and p["energy_closure_residual_fraction_max_abs"] is not None and p["energy_closure_residual_fraction_max_abs"] > .01]})
        print("Exported", species, "fit RMS pull", round(model["weighted_residual_RMS_pull"], 3), flush=True)
    write_csv(out / "qf_target_smooth.csv", smooth_rows, FIELDS)
    write_csv(out / "response_target_smooth.csv", response_rows)
    write_json(out / "response_models.json", {"models": models})
    export_header(out / "cygno_srim_response.hpp", models)
    point_index = [{"run_id": p["run_id"], "batch_ids": p["batch_ids"], "n_batches": p["n_batches"]} for p in points]
    write_json(out / "point_provenance.json", point_index)
    proxies = []
    with (snapshot / "srmodule.csv").open() as handle:
        for row in csv.DictReader(handle):
            if row["medium"] != "baseline" or not lo <= float(row["energy_kevnr"]) <= hi:
                continue
            species, z, mass = SPECIES[row["species"]]
            se, sn = float(row["electronic_stopping_mev_mg_cm2"]), float(row["nuclear_stopping_mev_mg_cm2"])
            proxies.append({"species": species, "Z": z, "A": mass, "energy_keV": float(row["energy_kevnr"]),
                            "electronic_stopping_MeV_mg_cm2": se, "nuclear_stopping_MeV_mg_cm2": sn,
                            "proxy_Se_over_Se_plus_Sn": se / (se + sn), "is_direct": False,
                            "method": "SRModule stopping-power proxy; NOT full-cascade QF; source gas293K"})
    write_csv(out / "stopping_power_proxies.csv", proxies)
    manifest = {"schema": "CYGNO-MC-SRIM-export-v1", "source_study": source_id,
                "target": target, "user_rounded_target_density_g_cm3": .00138850,
                "source_gas": gas, "source_simulation_settings": metadata["configuration"]["simulation"],
                "density_target_over_source": target["density_g_cm3"] / gas["density_g_cm3"],
                "condition_match": False,
                "target_transfer": "QF and channel energies reused unchanged under dilute homogeneous-gas density/temperature invariance approximation. No exact-target simulations or validation of that transfer were performed. Source and Geant4 natural target masses also differ slightly.",
                "uncertainty_not_quantified": ["density/temperature and natural-target-mass transfer", "SRIM physical model", "detector charge calibration", "smoothing shape/basis choice and regularization selection"],
                "coverage": coverage, "source_batch_count": len(records),
                "bounds_interpretation": "Numerically supported interpolation domain, not a validated detector-response or SRIM low-energy-physics interval",
                "physical_charge_calibrated": False,
                "energy_accounting_qualified": len(coverage)==5 and all('raw_energy_budget_flags' in c and not c['raw_energy_budget_flags'] for c in coverage),
                "flagged_low_energy_points_keV": [{"species":c['species'],**flag} for c in coverage for flag in c.get('raw_energy_budget_flags',[])],
                "nominal_source_ions": sum(b["job"]["n_ions"] for b in records),
                "method_labels": {"qf_source_direct.csv": "direct full-cascade aggregate at original293K gas conditions",
                                  "qf_target_transferred.csv": "derived density/temperature transfer, QF unchanged, original source history counts",
                                  "qf_target_smooth.csv": "derived constrained smooth regression; n_ions=0 means no new events at evaluation energies",
                                  "stopping_power_proxies.csv": "deterministic stopping ratio diagnostic, never used in QF fits"},
                "valid_energy_keV": [lo,hi], "grid_points_per_species": grid_points,
                "boundary_policy": f"Outside {lo:g}–{hi:g} keV per ion unsupported; evaluator raises. Exact-target direct coverage is missing throughout. No extrapolation/clipping.",
                "event_abundance": "Source counts are SRIM simulated histories, not Geant4 events or physical H/O abundances",
                "snapshot_sha256": sha(snapshot / "sha256.json"),
                "environment": {"numpy": np.__version__, "scipy": scipy.__version__},
                "exporter_sha256": sha(Path(__file__))}
    write_json(out / "manifest.json", manifest)
    make_plots(out, points, models)
    return manifest


def make_plots(out, points, models):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6), constrained_layout=True)
    colors = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00"]
    for model, color in zip(models, colors):
        e = np.geomspace(*model["valid_energy_keV"], 600)
        q, qs, d, ds = evaluate(model, e)
        axes[0].plot(e, q, color=color, label=model["species"])
        axes[0].fill_between(e, q - qs, q + qs, color=color, alpha=.15)
        raw = [p for p in points if p["species"] == model["species"] and model['valid_energy_keV'][0] <= p["energy_keV"] <= model['valid_energy_keV'][1]]
        axes[0].errorbar([p["energy_keV"] for p in raw], [p["QF"] for p in raw], yerr=[p["QF_sem"] or 0 for p in raw], color=color, fmt=".", ms=4, alpha=.8)
        axes[1].plot(e, d, color=color, label=model["species"])
        axes[1].fill_between(e, d - ds, d + ds, color=color, alpha=.15)
    for ax in axes:
        ax.set_xscale("log"); ax.set_xlabel("Total kinetic energy [keV per ion]")
        ax.grid(alpha=.25); ax.legend()
    axes[0].set_ylabel("Inclusive SRIM electronic-energy fraction QF")
    axes[1].set_ylabel("d[E QF(E)] / dE")
    fig.suptitle("CYGNO-MC: derived target transfer from 293 K SRIM; bands = conditional MC SEM")
    fig.savefig(out / "qf_and_derivative.png", dpi=170)
    fig.savefig(out / "qf_and_derivative.pdf")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", type=Path, default=Path("runs/cygno_hecf4_900mbar_20261015"))
    parser.add_argument("--config", type=Path, default=Path("geant4_config.json"))
    parser.add_argument("--snapshot", type=Path, help="Replay an immutable prior export snapshot")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bootstrap", type=int, default=400)
    parser.add_argument("--grid-points", type=int, default=401)
    parser.add_argument("--seed", type=int, default=20261016)
    args = parser.parse_args()
    if args.bootstrap < 30 or args.grid_points < 3:
        parser.error("Use at least30 bootstrap replicas and3 evaluation points")
    out = args.out.resolve()
    study = args.study_dir.resolve()
    if out == study or study in out.parents or out in study.parents:
        parser.error("Export must be outside the study tree")
    if out.exists():
        parser.error("Use a new output directory; exports are immutable")
    if args.snapshot and (out in args.snapshot.resolve().parents or args.snapshot.resolve() in out.parents):
        parser.error("Output and replay snapshot must not contain each other")
    out.mkdir(parents=True)
    if args.snapshot:
        load_snapshot(args.snapshot)
        shutil.copytree(args.snapshot, out / "snapshot")
    else:
        capture(study, args.config.resolve(), out / "snapshot")
    shutil.copy2(Path(__file__), out / "export_cygno_mc.py")
    build(out / "snapshot", out, args.bootstrap, args.grid_points, args.seed)
    print("Export ready:", out)


if __name__ == "__main__":
    main()
