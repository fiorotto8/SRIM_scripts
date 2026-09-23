#!/usr/bin/env python3
"""Bounded epsilon-sensitivity pilot; never launches or pools production."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan as scan
from scripts.low_energy_topup import TopupCampaign
from scripts.precision_scan import pending
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.repro_check import archive_data
from scripts.staged_scan import configured_jobs, validate_selected, write_json
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / "runs/binding_validation"
UNIT = "srim-binding-validation.service"
MODELS = ("epsilon_1e6", "epsilon_1e9")
CONFIGS = {name: ROOT / f"configs/binding_validation_{name}.toml" for name in MODELS}
BUDGET = 2 * 3600


def prepare(run):
    grids = {name: configured_jobs(path) for name, path in CONFIGS.items()}
    left, right = (grids[name][1] for name in MODELS)
    if len(left) != 36 or [j.payload for j in left] != [j.payload for j in right]:
        raise ValueError("Expected identical paired 36-point grids")
    if [j.job_id for j in left] != [j.job_id for j in right]:
        raise ValueError("Paired job IDs/seeds differ")
    if any(j.payload["n_ions"] != 5 or j.payload["repeat"] != 50 for j in left):
        raise ValueError("Pilot must remain five-ion, repeat-50 diagnostics")
    files = [*CONFIGS.values(), Path(__file__), ROOT / "srim_scan.py", ROOT / "pysrim_runner.py",
             ROOT / "scripts/low_energy_topup.py", ROOT / "scripts/staged_scan.py",
             ROOT / "scripts/run_ubuntu_7day.sh"]
    plan = dict(input_sha256={str(p.relative_to(ROOT)): scan.sha256_file(p) for p in files},
                model_ids=list(MODELS), jobs_per_model=36, paired_points=36, total_jobs=72,
                total_simulated_histories=360, ions_per_job=5,
                cumulative_budget_seconds=BUDGET,
                job_ids=[j.job_id for j in left],
                acceptance="All requested target energies confirmed, artifacts valid, no clipping; "
                           "paired seeds/geometry equal and RANGE/IONIZ byte-identical for every pair. "
                           "Any difference requires review, not automatic production release.",
                interpretation="Matched seeds intentionally shared across models. Not independent batches; "
                               "no SEM, gas-model accuracy or detector-quenching certification.")
    path = run / "plan.json"
    if path.exists():
        if json.loads(path.read_text()) != plan:
            raise ValueError("Frozen pilot inputs changed; do not resume this directory")
    else:
        write_json(path, plan)
    return plan, grids


def compare_pair(left, right, left_outputs, right_outputs):
    scalar_fields = ("random_seed", "n_ions", "ion", "family", "energy_kev",
                     "noble_percent", "pressure_atm", "target_width_angstrom", "target_width_attempts")
    same_inputs = all(left.get(k) is not None and left.get(k) == right.get(k) for k in scalar_fields)
    exact = all(left_outputs.get(k) is not None and left_outputs.get(k) == right_outputs.get(k)
                for k in ("RANGE.TXT", "IONIZ.TXT"))
    energies_match = all(row.get("target_energy_check", {}).get("passed") is True for row in (left, right))
    differences = {}
    for key in ("projected_range_mm", "qf_trim_ionization"):
        a, b = left[key], right[key]
        differences[key + "_relative_change"] = (b - a) / a if a else (0 if b == 0 else None)
    return dict(job_id=left["job_id"], family=left["family"], ion=left["ion"],
                energy_kev=left["energy_kev"], inputs_paired=same_inputs,
                reported_energies_match=energies_match, range_ioniz_byte_identical=exact,
                passed=same_inputs and exact and energies_match, **differences)


def comparisons(run):
    rows = {name: {r["job_id"]: r for r in completed_rows(run / name)[0]} for name in MODELS}
    result = []
    for job_id in sorted(set(rows[MODELS[0]]) & set(rows[MODELS[1]])):
        pair = [rows[name][job_id] for name in MODELS]
        hashes = [{output: hashlib.sha256(archive_data(run / name / row["raw_archive"], output)).hexdigest()
                   for output in ("RANGE.TXT", "IONIZ.TXT")} for name, row in zip(MODELS, pair)]
        result.append(compare_pair(*pair, *hashes))
    return result


def publish(run, state):
    progress = {}
    for model in MODELS:
        rows, counts = completed_rows(run / model)
        progress[model] = dict(completed=len(rows), expected=36, checkpoint=counts)
    pairs = comparisons(run)
    write_json(run / "progress.json", progress)
    write_json(run / "comparisons.json", pairs)
    atomic_text(run / "comparisons.csv", csv_text(pairs, list(pairs[0]) if pairs else ["pending"]))
    body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>SRIM binding validation</title><style>body{font:16px system-ui;max-width:1200px;margin:30px auto}
table{border-collapse:collapse}td,th{padding:7px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>Near-zero binding validation — production remains on hold</h1>
<p>Ar/Kr/Xe, 80% noble / 20% CF4, 1 atm; C/F/noble recoils; 1, 10, 100, 1000 keV.
Each of 36 points compares 1e-6 and 1e-9 eV bindings using matched seeds and five ions per model.
These are numerical diagnostics, NOT independent precision batches or calibrated detector quenching.</p>
<p>15 cores/workers maximum; 24/32 GiB soft/hard RAM; no scan swap; two cumulative hours.
Old scans are unchanged. There is no automatic production release.</p>
<p><a href="comparisons.csv">Paired results CSV</a> · <a href="comparisons.json">Full comparisons</a> ·
<a href="plan.json">Frozen plan</a> · <a href="logs/campaign.log">Logs</a></p>'''
    body += table([state], list(state)) + table([dict(model=k, **v) for k, v in progress.items()],
                                               ["model", "completed", "expected", "checkpoint"])
    body += table(pairs, ["family", "ion", "energy_kev", "reported_energies_match",
                          "range_ioniz_byte_identical", "inputs_paired", "passed"])
    atomic_text(run / "index.html", body)
    return progress, pairs


class ValidationCampaign(TopupCampaign):
    @property
    def budget_limit(self):
        return BUDGET

    def publish_report(self):
        return publish(self.run, self.state)

    def selected(self, name, config, run, stage, jobs):
        if not jobs:
            raise ValueError("Refuse empty selection: could release an unintended grid")
        todo = pending(run, jobs)
        if todo:
            self.phase(name, config, run, stage, todo)
        if pending(run, jobs):
            raise RuntimeError(f"{name}: jobs incomplete")

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            group = Path(resource_sample()["cgroup"])
            if group.name != UNIT or len(os.sched_getaffinity(0)) > 15:
                raise RuntimeError("Use binding_validation.sh for enforced resource limits")
            if ((group / "memory.max").read_text().strip() != str(32 * GIB)
                    or (group / "memory.swap.max").read_text().strip() != "0"):
                raise RuntimeError("Expected 32 GiB RAM and no scan swap")
            if memory_info()["MemAvailable"] < 16 * GIB or self.used >= BUDGET:
                raise RuntimeError("Insufficient memory headroom or cumulative budget")
            plan, grids = prepare(self.run)
            cfg = scan.load_config(CONFIGS[MODELS[0]])
            source = scan.resolve_srim_root(None, cfg)
            before = dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            provenance = dict(source=before, execution=scan.execution_provenance(scan.resolve_execution_context(cfg)))
            path = self.run / "source_provenance.json"
            if path.exists() and json.loads(path.read_text()) != provenance:
                raise RuntimeError("Source installation/provenance changed since preparation")
            write_json(path, provenance)
            write_json(self.run / f"session-{os.getpid()}.json", dict(
                commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                cpu_affinity=sorted(os.sched_getaffinity(0)), started_at=self.state["started_at"]))
            self.update(force=True)
            for model in MODELS:
                sr, jobs = grids[model]
                self.selected(f"{model}: stopping tables", CONFIGS[model], self.run / model, "srmodule", sr)
                self.selected(f"{model}: 36 five-ion target checks", CONFIGS[model], self.run / model, "trim", jobs)
                issues = validate_selected(self.run / model, jobs)
                for row in completed_rows(self.run / model)[0]:
                    report = scan.check_archived_target_energies(
                        self.run / model / row["raw_archive"],
                        scan.requested_target_energies(("C", "F", row["family"]), scan.load_config(CONFIGS[model])))
                    if not report["passed"]:
                        issues.append([row["job_id"], report["issues"]])
                write_json(self.run / f"{model}_validation.json", dict(issues=issues))
                if issues:
                    raise RuntimeError(f"{model}: validation failed; no subsequent model launched")
            prepare(self.run)
            unchanged = before == dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            pairs = comparisons(self.run)
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            passed = (len(pairs) == 36 and all(p["passed"] for p in pairs) and unchanged
                      and int(events.get("oom_kill", 0)) == 0)
            self.state.update(status="REVIEW_REQUIRED", phase="Fixed binding-validation pilot complete",
                              numerical_gate_passed=passed, paired_points=len(pairs),
                              pairs_passing=sum(p["passed"] for p in pairs), source_unchanged=unchanged,
                              production_released=False)
            self.update(force=True)
            write_json(self.run / "completion.json", self.state)
            return 0 if passed else 1
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping
                              else "NEEDS_ATTENTION", error=str(exc), production_released=False)
            print(f"Validation pilot stopped: {exc}", file=sys.stderr, flush=True)
            self.update(force=True)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    subprocess.run(["systemctl", "--user", "--no-block", "stop", UNIT], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "status"))
    args = parser.parse_args()
    if args.command == "status":
        unit = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "-p", "MemoryCurrent",
                               "-p", "MemorySwapCurrent"], capture_output=True, text=True)
        print(unit.stdout.strip())
        if (RUN / "status.json").exists():
            state = json.loads((RUN / "status.json").read_text())
            print(json.dumps(state, indent=2))
            if state["status"] in ("STARTING", "RUNNING") and ("ActiveState=active" not in unit.stdout
                    or time.time() - datetime.fromisoformat(state["updated_at"]).timestamp() > 300):
                print("WARNING: service inactive or heartbeat stale; inspect logs")
        for model in MODELS:
            rows, counts = completed_rows(RUN / model)
            print(f"{model}: {len(rows)}/36 TRIM checks complete; {counts}")
        print("Report:", RUN / "index.html")
        return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            plan, _ = prepare(RUN)
            print(json.dumps({k: v for k, v in plan.items() if k not in ("input_sha256", "job_ids")}, indent=2))
            return 0
        return ValidationCampaign(RUN).execute()


if __name__ == "__main__":
    raise SystemExit(main())
