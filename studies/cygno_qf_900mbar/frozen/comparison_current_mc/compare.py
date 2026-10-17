#!/usr/bin/env python3
"""Compare a frozen SRIM export with verified live CYGNO-MC source formulas.

Requires Python 3, NumPy, SciPy, Matplotlib and g++. No simulations are launched.
Example, from CYGNO-MC root after copying the release below tests/nuclear_recoils:
  python3 tests/nuclear_recoils/srim_results_20261016_final/comparison_current_mc/compare.py \
    --export-dir tests/nuclear_recoils/srim_results_20261016_final --mc-root . \
    --out /tmp/cygno-srim-comparison
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SPECIES = ["H1", "He4", "C12", "O16", "F19"]
LABELS = {"He4": "helium", "C12": "carbon", "F19": "flor"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path, data):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data[0]))
        writer.writeheader()
        writer.writerows(data)


def old_response(parameters, species, energy):
    if species not in parameters:
        return np.ones_like(energy), np.ones_like(energy)
    k, a, b = (parameters[species][p] for p in ["k", "a", "b"])
    f = k * (energy + a * energy ** b)
    q = f / (1 + f)
    return q, q + energy * k * (1 + a*b*energy**(b-1)) / (1+f)**2


def function_body(source, name):
    match = re.search(r"(?:Double_t|void)\s+" + re.escape(name) + r"\s*\([^;{}]*\)\s*\{", source)
    if not match:
        raise AssertionError("Missing source function: " + name)
    start, end, depth = match.start(), match.end(), 1
    while depth:
        if source[end] == "{":
            depth += 1
        elif source[end] == "}":
            depth -= 1
        end += 1
    return source[start:end]


def verify_cpp(source, parameters, energies, out):
    """Compile the complete unchanged live QF and derivative function bodies."""
    compiler = shutil.which("g++")
    if not compiler:
        raise RuntimeError("g++ is required to qualify live source algebra")
    lines = ["#include <cmath>", "#include <iostream>", "#include <iomanip>",
             "using Double_t=double; using std::pow;"]
    for species, label in LABELS.items():
        for parameter, value in parameters[species].items():
            lines.append(f"static const double {parameter}_{label}={value:.17g};")
        for stem in ["f", "fprime", "QF", "QFprime", "dRdE"]:
            lines.append(function_body(source, f"{stem}_{label}"))
    lines.append("int main(){std::cout<<std::setprecision(17);")
    expected = []
    for species, label in LABELS.items():
        q, d = old_response(parameters, species, energies)
        for energy, qi, di in zip(energies, q, d):
            lines.append(f'std::cout<<"{species} {energy:.17g} "<<QF_{label}({energy:.17g})<<" "<<dRdE_{label}({energy:.17g})<<"\\n";')
            expected.append((float(qi), float(di)))
    lines.append("}")
    cpp, binary = out / "check_live_formulas.cpp", out / "check_live_formulas"
    cpp.write_text("\n".join(lines) + "\n")
    subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True, capture_output=True)
    output = subprocess.check_output([str(binary)], text=True)
    binary.unlink()
    (out / "live_cpp_values.txt").write_text(output)
    actual = [tuple(map(float, line.split()[2:])) for line in output.splitlines()]
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-14)
    return {"compiled_source_cases": len(actual), "compiler": compiler,
            "compiler_version": subprocess.check_output([compiler, "--version"], text=True).splitlines()[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--mc-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    export, mc, out = (p.resolve() for p in [args.export_dir, args.mc_root, args.out])
    if out == mc or out == export:
        raise ValueError("Use a comparison subdirectory, not the MC or export root")
    config_path = export / "snapshot/geant4_config.json"
    config = json.loads(config_path.read_text())
    checks = []
    observed = {}
    for relative, expected in config["provenance"]["sha256"].items():
        actual = sha(mc / relative)
        if actual != expected:
            raise AssertionError("Live MC source differs from supplied configuration: " + relative)
        observed[relative] = actual
    checks.append("All12 configuration source/macro SHA256 values match live checkout")
    source = (mc / "src/CYGNOHit.cc").read_text()
    event_source = (mc / "src/CYGNOEventAction.cc").read_text()
    parameters = {}
    for species, label in LABELS.items():
        parameters[species] = {}
        for parameter in ["k", "a", "b"]:
            match = re.search(r"static\s+const\s+double\s+" + parameter + "_" + label + r"\s*=\s*([0-9.eE+-]+)\s*;", source)
            if not match:
                raise AssertionError("Missing live coefficient: " + parameter + "_" + label)
            parameters[species][parameter] = float(match[1])
        if parameters[species] != config["existing_QF"]["coefficients"][species]:
            raise AssertionError("Coefficients differ from supplied configuration")
    for name in ["ApplyQuenching", "ApplyQuenchingAvg"]:
        body = function_body(source, "CYGNOHit::" + name)
        if "1000080160" in body or "2212" in body:
            raise AssertionError("H/O code behavior changed")
    if not re.search(r"if\s*\(\s*pdg\s*>\s*1000000000\s*\)", event_source):
        raise AssertionError("Expected NR selection changed")
    if not re.search(r"GetKineticEne\(\)\s*<=\s*1\.", event_source):
        raise AssertionError("Expected QF/derivative hit switch changed")
    out.mkdir(parents=True, exist_ok=True)
    module_spec = importlib.util.spec_from_file_location("frozen_exporter", export / "export_cygno_mc.py")
    exporter = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(exporter)
    models = {m["species"]: m for m in json.loads((export / "response_models.json").read_text())["models"]}
    if set(models) != set(SPECIES):
        raise AssertionError("Missing fitted species")
    for model in models.values():
        if model["valid_energy_keV"] != [.25, 1000]:
            raise AssertionError("Unexpected response energy domain")
    energies = np.geomspace(.25, 1000, 801)
    selected = np.array([.25, 1., 10., 100., 1000.])
    cpp_checks = verify_cpp(source, parameters, np.unique(np.r_[np.geomspace(.25, 1000, 41), selected]), out)
    checks.append("Complete live C++ QF and dRdE functions agree with extracted Python algebra")
    finite_difference_checks = {}
    for species in SPECIES:
        energy = np.geomspace(.251, 999, 121)
        h = energy * 1e-5
        qp, _ = old_response(parameters, species, energy+h)
        qm, _ = old_response(parameters, species, energy-h)
        _, derivative = old_response(parameters, species, energy)
        numeric = ((energy+h)*qp-(energy-h)*qm)/(2*h)
        np.testing.assert_allclose(numeric, derivative, rtol=1e-8, atol=1e-10)
        trim_qp = exporter.evaluate(models[species], energy+h)[0]
        trim_qm = exporter.evaluate(models[species], energy-h)[0]
        trim_d = exporter.evaluate(models[species], energy)[2]
        trim_numeric = ((energy+h)*trim_qp-(energy-h)*trim_qm)/(2*h)
        np.testing.assert_allclose(trim_numeric, trim_d, rtol=1e-7, atol=1e-9)
        finite_difference_checks[species] = {
            "cases": len(energy), "current_max_absolute_error": float(max(abs(numeric-derivative))),
            "trim_max_absolute_error": float(max(abs(trim_numeric-trim_d)))}
    checks.append("Both full d[E*QF]/dE derivatives independently checked by finite differences at121energies/species")
    with (export / "qf_source_direct.csv").open() as handle:
        raw = list(csv.DictReader(handle))
    fig, axes = plt.subplots(2, 5, figsize=(17, 7), sharex=True, sharey="row")
    rows, samples, summary = [], [], {}
    for col, species in enumerate(SPECIES):
        model = models[species]
        q, qs, d, ds = exporter.evaluate(model, energies)
        oldq, oldd = old_response(parameters, species, energies)
        behavior = "proton_excluded_NR_raw_deposits" if species == "H1" else "oxygen_unity_fallback" if species == "O16" else "analytic_fit"
        for row, new, sem, old in [(0, q, qs, oldq), (1, d, ds, oldd)]:
            ax = axes[row, col]
            ax.plot(energies, new, color="#0072B2", label="Completed TRIM fit")
            ax.fill_between(energies, new-sem, new+sem, color="#0072B2", alpha=.25, label="Conditional MC SEM")
            ax.plot(energies, old, "--", color="#D55E00", label="Current unity code behavior" if species in ("H1", "O16") else "Current analytic formula")
            ax.set_xscale("log")
            ax.grid(alpha=.2)
            ax.set_ylim(0, 1.09)
            ax.set_xlim(.25, 1000)
        points = [r for r in raw if r["species"] == species]
        axes[0, col].errorbar([float(r["energy_keV"]) for r in points], [float(r["QF"]) for r in points],
                            yerr=[float(r["QF_sem"]) for r in points], fmt=".", ms=3, color="#0072B2", alpha=.65)
        axes[0, col].set_title(species + (" (H excluded from NR)" if species == "H1" else " (unity fallback)" if species == "O16" else ""))
        axes[1, col].set_xlabel("Energy [keV per ion]")
        for grid, container in [(energies, rows), (selected, samples)]:
            tq, ts, td, tds = exporter.evaluate(model, grid)
            cq, cd = old_response(parameters, species, grid)
            for i, energy in enumerate(grid):
                container.append({"species": species, "Z": model["Z"], "A": model["A"], "energy_keV": float(energy),
                    "QF_trim": float(tq[i]), "QF_trim_sem": float(ts[i]), "QF_current": float(cq[i]),
                    "QF_difference": float(tq[i]-cq[i]), "QF_relative_difference_percent": float(100*(tq[i]/cq[i]-1)),
                    "dR_dE_trim": float(td[i]), "dR_dE_trim_sem": float(tds[i]), "dR_dE_current": float(cd[i]),
                    "dR_dE_difference": float(td[i]-cd[i]), "current_behavior": behavior,
                    "current_hit_multiplier": float(cq[i] if energy <= 1 else cd[i]),
                    "trim_curve_is_direct": 0})
        idx = int(np.argmax(np.abs(q-oldq)))
        summary[species] = {"max_absolute_QF_difference": float(abs(q[idx]-oldq[idx])), "energy_keV_at_max": float(energies[idx]),
                            "current_behavior": behavior}
    axes[0, 0].set_ylabel("QF: inclusive electronic fraction")
    axes[1, 0].set_ylabel("d[E QF(E)] / dE")
    axes[0, 0].legend(fontsize=7, loc="lower right")
    fig.suptitle("Completed full-cascade TRIM fit vs current CYGNO-MC formulas\n293 K TRIM transferred to 293.15 K; H/O unity lines are code behavior, not fitted QFs", fontsize=12)
    fig.text(.5, .015, "Dots: direct TRIM points. Bands: conditional MC SEM only. Full derivatives shown; current hit code uses QF at E <= 1 keV.", ha="center", fontsize=9)
    fig.tight_layout(rect=[0, .04, 1, .91])
    for ext in ["png", "pdf"]:
        fig.savefig(out / f"qf_derivative_comparison.{ext}", dpi=180)
    plt.close(fig)
    write_csv(out / "comparison_grid.csv", rows)
    write_csv(out / "comparison_selected_energies.csv", samples)
    for relative, original_hash in observed.items():
        if sha(mc / relative) != original_hash:
            raise AssertionError("Live source changed during comparison: " + relative)
    provenance = {"created_UTC": datetime.now(timezone.utc).isoformat(), "MC_root_at_execution": str(mc),
        "CYGNO_MC_HEAD": subprocess.check_output(["git", "-C", str(mc), "rev-parse", "HEAD"], text=True).strip(),
        "config_source_HEAD": config["provenance"]["HEAD"], "config_source_matches_current_bytes": True,
        "verified_MC_source_sha256": observed, "parameters_extracted_from_live_source": parameters,
        "input_sha256": {name: sha(export / name) for name in ["response_models.json", "qf_source_direct.csv", "export_cygno_mc.py", "snapshot/geant4_config.json"]},
        "comparison_script_sha256": sha(Path(__file__)), "checks_passed": checks, "cpp_verification": cpp_checks,
        "finite_difference_checks": finite_difference_checks, "summary": summary,
        "scope": "Formula comparison only; no Geant4 events or detector calibration. H/O unity reflects code behavior. Legacy fit uncertainties are unavailable. TRIM curves/bands are derived fit and conditional MC SEM, not exact-target direct simulations or physical-model errors."}
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    (out / "README.md").write_text("# Completed TRIM versus current CYGNO-MC\n\n"
        "`qf_derivative_comparison.pdf` and `.png` compare all five species. Blue dots are source direct full-cascade TRIM points; blue curves/bands are the derived smooth response and conditional MC SEM. Orange dashed H/O lines represent existing code behavior, not fitted QFs. Other orange curves are the current source analytic fits.\n\n"
        "`comparison_grid.csv` contains 801 energies/species; `comparison_selected_energies.csv` contains 0.25, 1, 10, 100 and 1000 keV per ion. Derivative columns show full d[E QF]/dE; current_hit_multiplier also records the code's QF/derivative switch at 1 keV.\n\n"
        "Re-run after transfer with NumPy, SciPy, Matplotlib and g++ installed:\n\n```bash\n"
        "python3 comparison_current_mc/compare.py --export-dir . --mc-root /path/to/CYGNO-MC --out /tmp/cygno-srim-comparison\n```\n\n"
        "The script checks all source hashes from the supplied frozen configuration before comparing. It compiles the complete current C++ analytic functions and checks both current and fitted derivatives by independent finite differences. A source change requires a newly reviewed configuration. No source or simulation outputs are modified.\n\n"
        "This is an inclusive electronic-energy response comparison. Parent/secondary accounting, hydrogen selection, oxygen treatment and below-grid terminal policy still require review before per-hit integration. Legacy fit errors and physical detector calibration are unavailable.\n")
    print(json.dumps({"output": str(out), "passed": True, "rows": len(rows), "selected_rows": len(samples), "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
