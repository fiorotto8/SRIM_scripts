#!/usr/bin/env python3
"""Validate a CYGNO-MC export without opening or controlling a SRIM worker."""
import argparse
import csv
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from scipy.integrate import quad

from export_cygno_mc import FIELDS, SPECIES, evaluate, load_snapshot, sha, write_json


def rows(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


def validate(root, compare=None):
    config, _, records = load_snapshot(root / "snapshot")
    manifest = json.loads((root / "manifest.json").read_text())
    checks = []
    direct = rows(root / "qf_source_direct.csv")
    transferred = rows(root / "qf_target_transferred.csv")
    smooth = rows(root / "qf_target_smooth.csv")
    with (root / "qf_source_direct.csv").open() as handle:
        assert next(csv.reader(handle)) == FIELDS
    assert all(r["is_direct"] == "1" for r in direct)
    assert all(r["is_direct"] == "0" for r in transferred + smooth)
    for a, b in zip(direct, transferred):
        assert {k: v for k, v in a.items() if k != "is_direct"} == {k: v for k, v in b.items() if k != "is_direct"}
    assert len(direct) == len(transferred)
    assert {r["species"] for r in direct} == {v[0] for v in SPECIES.values()}
    checks.append("Requested CSV contract and all isotope identities; density-transfer values unchanged and labeled derived")
    for r in rows(root / "energy_channels_source.csv"):
        energy = float(r["energy_keV"])
        total = float(r["primary_electronic_keV"]) + float(r["recoil_electronic_keV"])
        np.testing.assert_allclose(total / energy, float(r["QF"]), rtol=1e-12)
        if r["QF_sem"]:
            variance = (float(r["primary_electronic_keV_sem"]) ** 2 + float(r["recoil_electronic_keV_sem"]) ** 2
                        + 2 * float(r["primary_recoil_mean_covariance_keV2"]))
            np.testing.assert_allclose(np.sqrt(max(0, variance)) / energy, float(r["QF_sem"]), rtol=1e-8, atol=1e-14)
    checks.append("Inclusive primary+recoil energy and SEM reproduce using channel covariance")
    models = json.loads((root / "response_models.json").read_text())["models"]
    model_map = {m["species"]: m for m in models}
    assert len(models) == 5
    for species, model in model_map.items():
        lo, hi = model['valid_energy_keV']
        curve = [r for r in smooth if r["species"] == species]
        energies = np.array([float(r["energy_keV"]) for r in curve])
        assert energies[0] == lo and energies[-1] == hi
        q, qs, d, ds = evaluate(model, energies)
        assert np.all((q >= -1e-10) & (q <= 1 + 1e-10)) and np.all(d >= -1e-10)
        assert np.all(np.diff(energies * q) > 0)
        assert np.all(qs >= 0) and np.all(ds >= 0)
        np.testing.assert_allclose(q, [float(r["QF"]) for r in curve], rtol=1e-12)
        interval_points = [float(np.exp(x)) for x in set(model["knots_log_energy"]) if lo < np.exp(x) < hi]
        integral = quad(lambda energy: float(evaluate(model, np.array([energy]))[2][0]), lo, hi,
                        points=interval_points, epsabs=1e-7)[0]
        np.testing.assert_allclose(integral, energies[-1] * q[-1] - energies[0] * q[0], rtol=1e-10)
        for energy in (lo*.99, hi*1.001, float("nan")):
            try:
                evaluate(model, np.array([energy]))
            except ValueError:
                pass
            else:
                raise AssertionError("Unsupported energy was accepted")
    checks.append("All five smooth models bounded and monotone; analytic derivative integrates to R(upper)-R(lower); outside-grid rejection")
    qualification = root / "qualification"
    qualification.mkdir(exist_ok=True)
    compiler = shutil.which("g++")
    if not compiler:
        raise RuntimeError("g++ is required for the C++ interchange qualification")
    cpp = qualification / "check_response.cpp"
    lines = ['#include "../cygno_srim_response.hpp"', '#include <iostream>', '#include <iomanip>',
             'int main(){ std::cout<<std::setprecision(17);']
    for model in models:
        for energy in np.geomspace(*model['valid_energy_keV'], 41):
            lines.append(f'{{auto r=cygno_srim::evaluate({model["Z"]},{model["A"]},{float(energy):.17g});std::cout<<"{model["species"]} {float(energy):.17g} "<<r.QF<<" "<<r.QF_sem<<" "<<r.dRdE<<" "<<r.dRdE_sem<<"\\n";}}')
    lo,hi=model_map['H1']['valid_energy_keV']
    lines += [f'for(double e:{{0.0,{lo*.999:.17g},{hi*1.001:.17g}}}) {{bool caught=false;try{{cygno_srim::evaluate(1,1,e);}}catch(const std::out_of_range&){{caught=true;}}if(!caught)return 2;}}',
              'bool caught=false;try{cygno_srim::evaluate(3,7,10);}catch(const std::invalid_argument&){caught=true;}return caught?0:3;}']
    cpp.write_text("\n".join(lines) + "\n")
    binary = qualification / "check_response"
    subprocess.run([compiler, "-std=c++17", "-O2", str(cpp), "-o", str(binary)], check=True, capture_output=True)
    output = subprocess.check_output([str(binary)], text=True)
    for line in output.splitlines():
        species, e, *values = line.split()
        expected = [float(v[0]) for v in evaluate(model_map[species], np.array([float(e)]))]
        np.testing.assert_allclose(list(map(float, values)), expected, rtol=1e-9, atol=1e-12)
    (qualification / "cpp_values.txt").write_text(output)
    checks.append("Compiled C++17 header agrees with Python on205 isotope/energy cases including endpoints; bounds and unsupported species throw")
    replay_files = []
    if compare:
        replay_files = [p.name for p in root.glob("*.csv")] + ["response_models.json", "point_provenance.json", "cygno_srim_response.hpp"]
        for name in replay_files:
            assert sha(root / name) == sha(compare / name), "Frozen replay differs: " + name
        checks.append("All numerical CSVs, spline coefficients/bootstrap draws and C++ header byte-identical on frozen replay")
    assert not manifest["condition_match"]
    assert all(c["exact_target_direct_coverage"] == [] for c in manifest["coverage"])
    checks.append("Exact-target direct coverage explicitly missing; source conditions preserved")
    result = {"passed": True, "checks": checks, "source_batches": len(records),
              "scope": "Numerical interchange only; not physical energy-closure or detector calibration qualification",
              "energy_accounting_qualified": manifest.get('energy_accounting_qualified', False),
              "valid_energy_keV": model_map['H1']['valid_energy_keV'],
              "source_ions": sum(b["job"]["n_ions"] for b in records), "direct_rows": len(direct),
              "smooth_rows": len(smooth), "target_temperature_K": config["target"]["temperature_K"],
              "replay_files": replay_files, "validator_sha256": sha(Path(__file__))}
    write_json(root / "validation.json", result)
    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    validate(args.export_dir.resolve(), args.compare.resolve() if args.compare else None)
