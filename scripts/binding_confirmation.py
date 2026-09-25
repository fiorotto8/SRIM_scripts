#!/usr/bin/env python3
"""Replay-gated independent-batch epsilon confirmation. Never releases production."""
from __future__ import annotations
import argparse
from datetime import datetime
import fcntl
import heapq
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan as scan
from scipy.stats import t
from scripts import binding_validation as pilot
from scripts.precision_scan import job_key
from scripts.repro_check import archive_data
from scripts.staged_scan import configured_jobs, validate_selected, write_json
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / "runs/binding_confirmation"
UNIT = "srim-binding-confirmation.service"
MODELS = pilot.MODELS
CONFIGS = {m: ROOT / f"configs/binding_confirmation_{m}.toml" for m in MODELS}
KEYS = {("Ar", 80, 1., "C", 1000.), ("Kr", 80, 1., "F", 1.),
        ("Xe", 80, 1., "F", 1000.), ("Kr", 80, 1., "Kr", 1000.), ("Xe", 80, 1., "Xe", 1000.)}
BUDGET = 4 * 3600
MARGINS = {"projected_range_mm": .005, "qf_trim_ionization": .002}
FILES = ("TRIM.IN", "TRIMAUTO", "Data/TRIM.cfg", "RANGE.TXT", "IONIZ.TXT", "TDATA.TXT")


def point(row):
    return row["family"], row["noble_percent"], row["pressure_atm"], row["ion"], row["energy_kev"]


def prepare(run):
    # This is deliberately a new plan, never an edit of the previous failed gate.
    old_plan = json.loads((pilot.RUN / "plan.json").read_text())
    for name, digest in old_plan["input_sha256"].items():
        if scan.sha256_file(ROOT / name) != digest:
            raise ValueError("Previous pilot frozen code/config changed")
    differences = [r for r in json.loads((pilot.RUN / "comparisons.json").read_text()) if not r["passed"]]
    if len(differences) != 5 or {point({**r, "noble_percent": 80, "pressure_atm": 1.}) for r in differences} != KEYS:
        raise ValueError("Expected exactly the five reviewed differing points")
    groups, hashes, forecast = {}, {}, 0.
    paths = [Path(__file__), ROOT / "scripts/binding_confirmation.sh", *CONFIGS.values(),
             pilot.RUN / "plan.json", pilot.RUN / "comparisons.json", pilot.RUN / "source_provenance.json"]
    for model in MODELS:
        old_run = pilot.RUN / model
        old_rows = completed_rows(old_run)[0]
        records = [r for r in old_rows if point(r) in KEYS]
        sr_old, old_jobs = configured_jobs(pilot.CONFIGS[model])
        replay = [j for j in old_jobs if j.job_id in {r["job_id"] for r in records}]
        sr, all_jobs = configured_jobs(CONFIGS[model])
        jobs = [j for j in all_jobs if job_key(j) in KEYS]
        if len(replay) != 5 or len(jobs) != 25:
            raise ValueError("Expected five replays and 25 fresh batches per model")
        old_seeds = {r["random_seed"] for r in old_rows}
        seeds = [scan.seed_for(j.job_id, j.payload["repeat"]) for j in jobs]
        if len(set(seeds)) != 25 or set(seeds) & old_seeds:
            raise ValueError("Fresh seeds collide or reuse pilot seeds")
        for key in KEYS:
            selected = [j for j in jobs if job_key(j) == key]
            if {j.payload["repeat"] for j in selected} != set(range(60, 65)) or any(j.payload["n_ions"] != 50 for j in selected):
                raise ValueError("Expected five independent 50-ion batches per point")
        pairs = {(j.payload["condition"]["condition_id"], j.payload["ion"]) for j in jobs}
        stopping = [j for j in sr if (j.payload["condition"]["condition_id"], j.payload["ion"]) in pairs]
        stopping_old = [j for j in sr_old if (j.payload["condition"]["condition_id"], j.payload["ion"]) in pairs]
        groups[model] = dict(records=records, replay=replay, sr_replay=stopping_old, jobs=jobs, sr=stopping)
        paths.extend(old_run / r["raw_archive"] for r in records)
        paths.extend(old_run / "job_results/trim" / f"{r['job_id']}.json" for r in records)
        costs = {point(r): r["wall_seconds_last_attempt"] for r in records}
        workers = [0.] * 15
        for cost in sorted((8 + max(1, costs[job_key(j)] - 8) * 10 for j in jobs), reverse=True):
            heapq.heapreplace(workers, workers[0] + cost)
        forecast += max(costs.values()) + 1.5 * max(workers)
    if [j.payload for j in groups[MODELS[0]]["jobs"]] != [j.payload for j in groups[MODELS[1]]["jobs"]]:
        raise ValueError("Fresh batches do not pair across models")
    hashes.update({str(p.relative_to(ROOT)): scan.sha256_file(p) for p in paths})
    plan = dict(input_sha256=hashes, previous_frozen_hashes=old_plan["input_sha256"],
                replay_ids={m: [j.job_id for j in g["replay"]] for m, g in groups.items()},
                fresh_ids={m: [j.job_id for j in g["jobs"]] for m, g in groups.items()},
                replay_jobs=10, fresh_jobs=50, fresh_histories=2500, independent_batch_pairs_per_point=5,
                budget_hours=4, planning_hours=(forecast + 900) / 3600,
                replay_gate="Every fixed-epsilon input/output replay must be byte-identical before any fresh batches.",
                statistical_gate=dict(relative_margins=MARGINS, family_alpha=.05, endpoints=10,
                    method="Mean within-pair relative difference (epsilon_1e9 - epsilon_1e6)/epsilon_1e6; "
                           "two-sided Student-t intervals, Bonferroni across 5 points x 2 observables. "
                           "Every interval must lie strictly within its margin. Five independent pairs/point; "
                           "assumes approximately normal independent batch differences; not distribution-free coverage."),
                limits="No optional stopping on precision, no automatic extra batches, no model pooling, no production release.")
    path = run / "plan.json"
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError("Frozen confirmation plan changed")
    if not path.exists():
        write_json(path, plan)
    return plan, groups


def paired_interval(values, margin):
    if len(values) != 5 or any(not math.isfinite(v) for v in values):
        raise ValueError("Expected five finite independent paired differences")
    mean = statistics.mean(values)
    sem = statistics.stdev(values) / math.sqrt(5)
    half = float(t.ppf(1 - .05 / (2 * 10), df=4)) * sem
    return dict(mean_relative_difference=mean, relative_sem=sem, lower=mean-half, upper=mean+half,
                margin=margin, passed=(mean-half > -margin and mean+half < margin))


def compare_replay(source, destination, old, new):
    equality = {name: archive_data(source / old["raw_archive"], name)
                      == archive_data(destination / new["raw_archive"], name) for name in FILES}
    return dict(job_id=old["job_id"], file_equality=equality, passed=all(equality.values()))


def analyze(run):
    rows = {m: completed_rows(run / "fresh" / m)[0] for m in MODELS}
    output = []
    for key in sorted(KEYS):
        cohorts = [{r["repeat"]: r for r in rows[m] if point(r) == key} for m in MODELS]
        if any(set(c) != set(range(60, 65)) for c in cohorts):
            continue
        pairs = [(cohorts[0][i], cohorts[1][i]) for i in range(60, 65)]
        seeds = [a["random_seed"] for a, b in pairs]
        if len(set(seeds)) != 5 or any(a["random_seed"] != b["random_seed"] or a["n_ions"] != 50 or b["n_ions"] != 50
                or a["target_width_angstrom"] != b["target_width_angstrom"] for a, b in pairs):
            raise ValueError("Broken history independence, pairing or geometry")
        for metric, margin in MARGINS.items():
            values = [(b[metric] - a[metric]) / a[metric] for a, b in pairs]
            output.append(dict(family=key[0], ion=key[3], energy_kev=key[4], metric=metric,
                               independent_pairs=5, **paired_interval(values, margin)))
    return output


class ConfirmationCampaign(pilot.ValidationCampaign):
    @property
    def budget_limit(self):
        return BUDGET

    def publish_report(self):
        progress = [dict(cohort=c, model=m, completed=len(completed_rows(self.run / c / m)[0]), expected=5 if c=="replay" else 25)
                    for c in ("replay", "fresh") for m in MODELS]
        try:
            results = analyze(self.run)
        except (ValueError, ZeroDivisionError) as exc:
            results = []
            self.state["statistics_error"] = str(exc)
        write_json(self.run / "progress.json", progress)
        write_json(self.run / "paired_statistics.json", results)
        atomic_text(self.run / "paired_statistics.csv", csv_text(results, list(results[0]) if results else ["pending"]))
        body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Binding confirmation</title><h1>Replay-gated binding confirmation</h1>
<p>Ten fixed-epsilon replays must reproduce before 50 fresh 50-ion batches are released.
Five independent batch pairs at each of five reviewed points. Matched seeds across epsilon models;
replays/pilot histories are excluded from statistics. No production is released.</p>
<p>Margins: range 0.5%, integrated ionization fraction 0.2%. Simultaneous nominal 95% family
coverage using Bonferroni Student-t intervals (5 pairs, 10 endpoints); normal batch-difference assumption.
This does not certify mixture endpoints, depth-profile equivalence, physical gas accuracy or detector quenching.</p>
<p>15 cores, RAM soft/hard 24/32 GiB, zero scan swap, four cumulative hours.</p>
<p><a href="replay_gate.json">Replay gate</a> · <a href="paired_statistics.csv">Paired statistics</a> ·
<a href="plan.json">Frozen plan</a> · <a href="logs/campaign.log">Logs</a></p>'''
        atomic_text(self.run / "index.html", body + table([self.state], list(self.state)) + table(progress, list(progress[0]))
                    + table(results, ["family", "ion", "energy_kev", "metric", "lower", "upper", "margin", "passed"]))

    def audit(self, directory, jobs, config):
        issues = validate_selected(directory, jobs)
        for row in completed_rows(directory)[0]:
            report = scan.check_archived_target_energies(directory / row["raw_archive"],
                      scan.requested_target_energies(("C", "F", row["family"]), scan.load_config(config)))
            if not report["passed"]:
                issues.append([row["job_id"], report["issues"]])
        if issues:
            raise RuntimeError(f"Artifact/physics validation failed at {directory}: {issues[:3]}")

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            group = Path(resource_sample()["cgroup"])
            if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15
                    or (group / "memory.max").read_text().strip() != str(32*GIB)
                    or (group / "memory.swap.max").read_text().strip() != "0"):
                raise RuntimeError("Use resource-capped binding_confirmation.sh")
            if memory_info()["MemAvailable"] < 16*GIB or self.used >= BUDGET:
                raise RuntimeError("Insufficient memory or cumulative budget")
            plan, groups = prepare(self.run)
            cfg = scan.load_config(CONFIGS[MODELS[0]])
            source = scan.resolve_srim_root(None, cfg)
            before = dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            previous = json.loads((pilot.RUN / "source_provenance.json").read_text())
            if previous != dict(source=before, execution=scan.execution_provenance(scan.resolve_execution_context(cfg))):
                raise RuntimeError("Source/execution changed since original pilot")
            write_json(self.run / f"session-{os.getpid()}.json", dict(source=before,
                       commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                       cpu_affinity=sorted(os.sched_getaffinity(0)), started_at=self.state["started_at"]))
            replays = []
            for model, g in groups.items():
                destination = self.run / "replay" / model
                self.audit(pilot.RUN / model, g["replay"], pilot.CONFIGS[model])
                self.selected(model + " replay stopping", pilot.CONFIGS[model], destination, "srmodule", g["sr_replay"])
                self.selected(model + " five fixed-epsilon replays", pilot.CONFIGS[model], destination, "trim", g["replay"])
                self.audit(destination, g["replay"], pilot.CONFIGS[model])
                mapping = {r["job_id"]: r for r in completed_rows(destination)[0]}
                for old in g["records"]:
                    new = mapping[old["job_id"]]
                    replays.append(dict(model=model, **compare_replay(pilot.RUN / model, destination, old, new)))
                write_json(self.run / "replay_gate.json", dict(passed=len(replays)==10 and all(r["passed"] for r in replays), comparisons=replays))
                if not all(r["passed"] for r in replays):
                    raise RuntimeError("Fixed-epsilon replay differs; no fresh batches released")
            if len(replays) != 10:
                raise RuntimeError("Replay gate incomplete")
            prepare(self.run)
            self.state["replay_gate_passed"] = True
            self.update(force=True)
            for model, g in groups.items():
                destination = self.run / "fresh" / model
                self.selected(model + " fresh stopping", CONFIGS[model], destination, "srmodule", g["sr"])
                self.selected(model + " 25 independent 50-ion batches", CONFIGS[model], destination, "trim", g["jobs"])
                self.audit(destination, g["jobs"], CONFIGS[model])
            prepare(self.run)
            unchanged = before == dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            results = analyze(self.run)
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            passed = len(results)==10 and all(r["passed"] for r in results) and unchanged and int(events.get("oom_kill",0))==0
            self.state.update(status="REVIEW_REQUIRED", phase="Fixed confirmation study complete", statistical_gate_passed=passed,
                              endpoints_passing=sum(r["passed"] for r in results), expected_endpoints=10,
                              source_unchanged=unchanged, production_released=False)
            self.update(force=True)
            write_json(self.run / "completion.json", self.state)
            return 0 if passed else 1
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping else "NEEDS_ATTENTION",
                              error=str(exc), production_released=False)
            print(str(exc), file=sys.stderr, flush=True)
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
        unit = subprocess.run(["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "-p", "MemoryCurrent", "-p", "MemorySwapCurrent"], capture_output=True, text=True)
        print(unit.stdout.strip())
        if (RUN / "status.json").exists():
            state = json.loads((RUN / "status.json").read_text())
            print(json.dumps(state, indent=2))
            if state["status"] in ("RUNNING", "STARTING") and ("ActiveState=active" not in unit.stdout or time.time()-datetime.fromisoformat(state["updated_at"]).timestamp()>300):
                print("WARNING: inactive service or stale heartbeat; inspect logs")
        for cohort in ("replay", "fresh"):
            for model in MODELS:
                rows, counts = completed_rows(RUN / cohort / model)
                print(cohort, model, len(rows), counts)
        print("Report:", RUN / "index.html")
        return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            plan, _ = prepare(RUN)
            print(json.dumps({k:v for k,v in plan.items() if k not in ("input_sha256", "previous_frozen_hashes", "replay_ids", "fresh_ids")}, indent=2))
            return 0
        return ConfirmationCampaign(RUN).execute()


if __name__ == "__main__":
    raise SystemExit(main())
