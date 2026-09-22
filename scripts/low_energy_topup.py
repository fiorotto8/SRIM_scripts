#!/usr/bin/env python3
"""Fixed 600-ion addition to 64 low-energy points; previous campaigns read-only."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import fcntl
import hashlib
import heapq
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import sqlite3
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan
from scripts import precision_analysis as analysis
from scripts.precision_scan import job_key, pending
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_report import write_root, energy_figure, scan_figure
from scripts.staged_scan import Campaign, configured_jobs, validate_selected, write_json
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / "runs/noble_cf4_low_energy_topup"
PREVIOUS = ROOT / "runs/noble_cf4_precision"
BASELINE = ROOT / "runs/noble_cf4_staged/production"
CONFIG = ROOT / "configs/noble_cf4_low_energy_topup.toml"
UNIT = "srim-low-energy-topup.service"
BUDGET = 2 * 3600


def prior_rows():
    return analysis.read_rows(BASELINE, PREVIOUS)


def select_jobs(rows, jobs):
    flagged = [r for r in analysis.points(rows) if not r["precision_met"]]
    if len(flagged) != 64 or any(r["n_ions_total"] != 400 or r["energy_kev"] not in (1, 3, 10) for r in flagged):
        raise ValueError("Expected the reviewed 64 low-energy points, each with 400 ions")
    keys = {analysis.key(r) for r in flagged}
    selected = [j for j in jobs if job_key(j) in keys]
    grouped = defaultdict(list)
    seeds = defaultdict(set)
    old_ids = {r["job_id"] for r in rows}
    for row in rows:
        seeds[analysis.key(row)].add(row["random_seed"])
    for job in selected:
        key = job_key(job)
        seed = srim_scan.seed_for(job.job_id, job.payload["repeat"])
        if job.job_id in old_ids or seed in seeds[key]:
            raise ValueError("A selected batch would reuse a previous history seed")
        seeds[key].add(seed)
        grouped[key].append(job)
    if set(grouped) != keys or any(len(batch) != 12 or
            {j.payload["repeat"] for j in batch} != set(range(10, 22)) or
            any(j.payload["n_ions"] != 50 for j in batch) for batch in grouped.values()):
        raise ValueError("Every selected point must receive exactly twelve fresh 50-ion batches")
    return selected


def prepare(run):
    sources = (CONFIG, BASELINE / "trim_results.csv", PREVIOUS / "refinement/trim_results.csv",
               PREVIOUS / "completion.json")
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    completion = json.loads((PREVIOUS / "completion.json").read_text())
    if completion["status"] != "REVIEW_REQUIRED" or completion["summary"]["points_present"] != 1890:
        raise ValueError("Expected the completed, reviewed 1890-point campaign")
    path = run / "plan.json"
    sr, jobs = configured_jobs(CONFIG)
    rows = prior_rows()
    selected = select_jobs(rows, jobs)
    pairs = {(j.payload["condition"]["condition_id"], j.payload["ion"]) for j in selected}
    stopping = [j for j in sr if (j.payload["condition"]["condition_id"], j.payload["ion"]) in pairs]
    times = defaultdict(list)
    for row in rows:
        times[analysis.key(row)].append(row["wall_seconds_last_attempt"])
    workers = [0.] * 15
    for cost in sorted((statistics.mean(times[job_key(j)]) for j in selected), reverse=True):
        heapq.heapreplace(workers, workers[0] + cost)
    plan = dict(input_hashes=hashes, job_ids=[j.job_id for j in selected],
                srmodule_job_ids=[j.job_id for j in stopping], selected_points=64,
                new_batches=len(selected), new_ions=50 * len(selected), target_ions_per_point=1000,
                model_minutes=max(workers) / 60, planning_minutes=1.5 * max(workers) / 60 + 10,
                cumulative_budget_hours=2, selection="Fixed 64 reviewed points; no further adaptive expansion")
    if path.exists():
        if json.loads(path.read_text()) != plan:
            raise ValueError("Frozen plan or inputs changed; do not resume this run")
    else:
        write_json(path, plan)
    return plan, stopping, selected


def read_new(run):
    rows, _ = completed_rows(run / "refinement")
    for row in rows:
        row["source_run"] = str(run / "refinement")
        row["original_energy_kev"] = row["energy_kev"]
        row["energy_kev"] = round(row["energy_kev"], 9)
        row["raw_archive"] = str((run / "refinement" / row["raw_archive"]).resolve())
    return rows


def publish(run, state, final=False):
    old = prior_rows()
    new = read_new(run)
    combined = analysis.points(old + new)
    independent = analysis.points(new)
    unresolved = [r for r in combined if not r["precision_met"]]
    summary = dict(expected_points=1890, points_present=len(combined), completed_new_batches=len(new),
                   expected_new_batches=768, points_at_1000_ions=sum(r["n_ions_total"] == 1000 for r in combined),
                   points_meeting_statistical_targets=sum(r["precision_met"] for r in combined),
                   unresolved_precision_points=len(unresolved),
                   independent_600_ion_points_complete=sum(r["n_ions_total"] == 600 for r in independent),
                   independent_600_ion_points_meeting_targets=sum(r["n_ions_total"] == 600 and r["precision_met"] for r in independent))
    output = run / "report"
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("points", combined), ("precision_flagged", unresolved),
                       ("independent_new_points", independent), ("new_batches", new)):
        atomic_text(output / f"{name}.csv", csv_text(data, list(data[0]) if data else ["no_rows"]))
    write_json(run / "science_status.json", summary)
    if final:
        from matplotlib.backends.backend_pdf import PdfPages
        import matplotlib.pyplot as plt
        write_root(output / "scan.root", old + new, combined)
        with PdfPages(output / "summary.pdf") as pdf:
            for name, fig in (("energy", energy_figure(combined, 80, 1)),
                              ("mixtures", scan_figure(combined, "noble_percent")),
                              ("pressure", scan_figure(combined, "pressure_atm"))):
                fig.savefig(output / f"{name}.png", dpi=120)
                pdf.savefig(fig)
                plt.close(fig)
    body = f'''<!doctype html><html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>SRIM low-energy precision follow-up</title>
<style>body{{font:16px system-ui;max-width:1200px;margin:30px auto}}table{{border-collapse:collapse}}td,th{{padding:6px;border:1px solid #ddd}}.scroll{{overflow:auto;max-height:500px}}img{{max-width:100%}}</style>
<h1>SRIM low-energy precision follow-up</h1>
<p>Fixed addition of twelve independent 50-ion batches at 64 selected points: 400 → 1000 ions/point.
Other points and both previous campaigns are unchanged. Limits: 15 workers, 32 GiB RAM, zero scan swap, two cumulative hours.</p>
<p>Targets: estimated relative SEM ≤5% for projected range and ≤2% for SRIM ionization fraction.
Pooled errors/intervals are descriptive after adaptive selection, not guaranteed coverage or detector quenching accuracy.
The independent-new-points table uses only the new 600 ions, not the data used to select these points;
its lower sample size can leave a larger SEM. No repeated stopping on favorable precision estimates.</p>
<p>This point-wise top-up does not redo pressure hypothesis tests; use the previous fixed-cohort pressure assessment.</p>
<p><a href="points.csv">Combined 1890-point CSV</a> · <a href="precision_flagged.csv">Unresolved points</a> ·
<a href="independent_new_points.csv">Independent new-batch estimates</a> · <a href="new_batches.csv">New batches</a> ·
<a href="../../noble_cf4_precision/report/index.html">Previous report and pressure checks</a> · <a href="../plan.json">Frozen plan</a></p>
{table([state], list(state))}{table([summary], list(summary))}
<h2>Remaining precision issues</h2>{table(unresolved, ['family','noble_percent','pressure_atm','ion','energy_kev','n_ions_total','projected_range_mm_relative_sem','qf_trim_ionization_relative_sem'])}
'''
    if final:
        body += '<p><a href="summary.pdf">Summary PDF</a> · <a href="scan.root">Combined ROOT file</a></p><img src="energy.png"><img src="mixtures.png"><img src="pressure.png">'
    atomic_text(output / "index.html", body + "</html>")
    return summary


class TopupCampaign(Campaign):
    @property
    def budget_limit(self):
        return BUDGET

    @property
    def config_path(self):
        return CONFIG

    def publish_report(self):
        return publish(self.run, self.state)

    def __init__(self, run):
        super().__init__(run)
        path = run / "budget.json"
        self.used = json.loads(path.read_text())["used_seconds"] if path.exists() else 0.
        self.tick = time.monotonic()
        self.budget_stopped = False

    def update(self, force=False):
        now = time.monotonic()
        self.used += now - self.tick
        self.tick = now
        if self.used >= self.budget_limit and not self.stopping:
            self.budget_stopped = True
            self.stop(signal.SIGTERM, None)
        sample = resource_sample()
        self.state.update(updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          cumulative_hours=round(self.used / 3600, 4), resources=sample)
        write_json(self.run / "budget.json", dict(used_seconds=self.used, limit_seconds=self.budget_limit))
        write_json(self.run / "status.json", self.state)
        import csv
        with (self.run / "resource_samples.csv").open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sample))
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(sample)
        if force or now - self.last_report > 120:
            self.publish_report()
            self.last_report = time.monotonic()

    def selected_phase(self, name, jobs, stage):
        if not jobs:
            return
        todo = pending(self.run / "refinement", jobs)
        if todo:
            self.phase(name, self.config_path, self.run / "refinement", stage, todo)
        if pending(self.run / "refinement", jobs):
            raise RuntimeError(f"{name}: required jobs incomplete")

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            group = Path(resource_sample()["cgroup"])
            if group.name != UNIT or len(os.sched_getaffinity(0)) > 15:
                raise RuntimeError("Launch with low_energy_topup.sh for enforced limits")
            if ((group / "memory.max").read_text().strip() != str(32 * GIB) or
                    (group / "memory.swap.max").read_text().strip() != "0"):
                raise RuntimeError("Expected 32 GiB RAM limit and zero scan swap")
            if memory_info()["MemAvailable"] < 16 * GIB or self.used >= BUDGET:
                raise RuntimeError("Insufficient host RAM or cumulative time budget")
            plan, sr, jobs = prepare(self.run)
            write_json(self.run / f"session-{os.getpid()}.json", dict(
                commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                cpu_affinity=sorted(os.sched_getaffinity(0)), started_at=self.state["started_at"],
                packages={p: importlib.metadata.version(p) for p in ("numpy", "scipy", "uproot", "matplotlib")}))
            self.state.update(status="RUNNING", phase="Read-only audit of selected prior batches")
            self.update(force=True)
            keys = {job_key(j) for j in jobs}
            for source, config in ((BASELINE, ROOT / "configs/noble_cf4_production.toml"),
                                   (PREVIOUS / "refinement", ROOT / "configs/noble_cf4_precision.toml")):
                existing, _ = completed_rows(source)
                ids = {r["job_id"] for r in existing if analysis.key(r) in keys}
                _, candidates = configured_jobs(config)
                checked = [j for j in candidates if j.job_id in ids]
                issues = validate_selected(source, checked)
                if len(checked) != len(ids) or issues:
                    raise RuntimeError(f"Read-only input audit failed for {source}: {issues[:5]}")
            todo = pending(self.run / "refinement", jobs)
            remaining_estimate = plan["planning_minutes"] * 60 * len(todo) / len(jobs)
            if remaining_estimate + self.used > BUDGET:
                raise RuntimeError("Remaining planned work does not fit the cumulative budget")
            self.selected_phase("Selected low-energy stopping tables", sr, "srmodule")
            self.selected_phase("64 points: twelve new 50-ion batches each", jobs, "trim")
            prepare(self.run)  # Recheck immutable inputs before final publication.
            issues = validate_selected(self.run / "refinement", jobs)
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            if int(events.get("oom_kill", 0)):
                issues.append(["resources", "OOM kill occurred"])
            write_json(self.run / "validation.json", dict(expected_batches=len(jobs), issues=issues, memory_events=events))
            if issues:
                raise RuntimeError("New output validation failed; inspect validation.json")
            summary = publish(self.run, self.state)
            if summary["points_present"] != 1890 or summary["points_at_1000_ions"] != 64 or summary["completed_new_batches"] != 768:
                raise RuntimeError("Final grid or history counts do not match the frozen plan")
            self.state.update(status="REVIEW_REQUIRED" if summary["unresolved_precision_points"] else "TARGETS_MET",
                              phase="Fixed low-energy follow-up complete")
            self.update(force=True)
            summary = publish(self.run, self.state, final=True)
            write_json(self.run / "completion.json", dict(status=self.state["status"], summary=summary,
                       note="All scheduled artifacts validated; precision is statistical, not detector/model accuracy."))
            return 0
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping else "NEEDS_ATTENTION", error=str(exc))
            print(f"Follow-up stopped: {exc}", file=sys.stderr, flush=True)
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
            if state["status"] == "RUNNING" and ("ActiveState=active" not in unit.stdout or time.time() - datetime.fromisoformat(state["updated_at"]).timestamp() > 300):
                print("WARNING: inactive service or stale heartbeat; inspect logs")
        db = RUN / "refinement/jobs.sqlite"
        if db.exists():
            with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as connection:
                print(connection.execute("SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status").fetchall())
        if (RUN / "science_status.json").exists():
            print((RUN / "science_status.json").read_text())
        print("Report:", RUN / "report/index.html")
        return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "prepare":
            plan, _, _ = prepare(RUN)
            print(json.dumps({k: v for k, v in plan.items() if k not in ("job_ids", "srmodule_job_ids", "input_hashes")}, indent=2))
            return 0
        return TopupCampaign(RUN).execute()


if __name__ == "__main__":
    raise SystemExit(main())
