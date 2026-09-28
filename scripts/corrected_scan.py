#!/usr/bin/env python3
"""Endpoint-gated, corrected-model 108-point scan with audited read-only reuse."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime
import fcntl
import heapq
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
import srim_scan as scan
from scripts import binding_confirmation as confirmation
from scripts import precision_analysis as analysis
from scripts.precision_scan import job_key
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import configured_jobs, write_json
from scripts.staged_report import write_root, energy_figure, scan_figure
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / "runs/noble_cf4_corrected_coarse"
UNIT = "srim-corrected-scan.service"
CONFIG = ROOT / "configs/noble_cf4_corrected_coarse.toml"
SOURCE = confirmation.RUN / "fresh/epsilon_1e9"
SOURCE_CONFIG = confirmation.CONFIGS["epsilon_1e9"]
MODEL = "noble-cf4-nearzero-1e9-v1"
BUDGET = 8 * 3600


def select_jobs(jobs, reused):
    keys = {analysis.key(r) for r in reused}
    if keys != confirmation.KEYS or len(reused) != 25:
        raise ValueError("Reuse must contain exactly the five accepted corrected-model points")
    for key in keys:
        rows = [r for r in reused if analysis.key(r) == key]
        if len(rows) != 5 or {r["repeat"] for r in rows} != set(range(60, 65)) or any(r["n_ions"] != 50 for r in rows):
            raise ValueError("Invalid reused batch cohort")
    fresh = [j for j in jobs if job_key(j) not in keys]
    endpoints = [j for j in fresh if j.payload["condition"]["noble_percent"] in (60, 95)
                 and j.payload["energy_kev"] in (1, 1000) and j.payload["repeat"] in (70, 71)]
    endpoint_ids = {j.job_id for j in endpoints}
    rest = [j for j in fresh if j.job_id not in endpoint_ids]
    if len(fresh) != 412 or len(endpoints) != 72 or len(rest) != 340:
        raise ValueError("Expected exactly 412 fresh batches: 72 endpoint checks + 340 remaining")
    cohorts = defaultdict(list)
    for j in fresh:
        cohorts[job_key(j)].append(j)
    if len(cohorts) != 103 or any({j.payload["repeat"] for j in group} != set(range(70, 74))
            or any(j.payload["n_ions"] != 50 for j in group) for group in cohorts.values()):
        raise ValueError("Each new point must have four fresh 50-ion batches")
    seeds = [r["random_seed"] for r in reused] + [scan.seed_for(j.job_id, j.payload["repeat"]) for j in fresh]
    if len(set(seeds)) != len(seeds):
        raise ValueError("Duplicate/reused history seeds")
    return fresh, endpoints, rest


def estimate(endpoints, rest, reused):
    """Legacy times are timing references only; no legacy physics is imported."""
    baseline = ROOT / "runs/noble_cf4_staged/production"
    times = defaultdict(list)
    connection = sqlite3.connect((baseline / "jobs.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        records = connection.execute("SELECT result_path,started_at,finished_at FROM jobs WHERE stage='trim' AND status='complete'").fetchall()
    finally:
        connection.close()
    for path, start, end in records:
        row = json.loads((baseline / path).read_text())
        if row["pressure_atm"] == 1 and row["noble_percent"] in (60, 80, 95):
            times[analysis.key(row)].append((datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds())
    costs = {k: statistics.mean(v) for k, v in times.items()}
    corrections = {f: 1. for f in ("Ar", "Kr", "Xe")}
    for key in {analysis.key(r) for r in reused}:
        measured = statistics.mean(r["wall_seconds_last_attempt"] for r in reused if analysis.key(r) == key)
        corrections[key[0]] = max(corrections[key[0]], measured / costs[key])
    durations = []
    for phase in (endpoints, rest):
        workers = [0.] * 15
        for cost in sorted((costs[job_key(j)] * corrections[job_key(j)[0]] for j in phase), reverse=True):
            heapq.heapreplace(workers, workers[0] + cost)
        durations.append(max(workers) / 3600)
    return dict(phase_model_hours=durations, planning_hours=1.5*sum(durations)+.5,
                family_corrections=corrections,
                assumptions="Historical 50-ion times, conservative measured family correction; two sequential "
                            "15-worker phases; 50% margin plus 30 minutes setup/reporting. Not a guarantee.")


def prepare(run):
    completion = json.loads((confirmation.RUN / "completion.json").read_text())
    if not all(completion.get(k) is True for k in ("replay_gate_passed", "statistical_gate_passed", "source_unchanged")):
        raise ValueError("Confirmation gates have not passed")
    previous_plan = json.loads((confirmation.RUN / "plan.json").read_text())
    frozen = {**previous_plan["input_sha256"], **previous_plan["previous_frozen_hashes"]}
    for path, digest in frozen.items():
        if scan.sha256_file(ROOT / path) != digest:
            raise ValueError("Confirmation frozen inputs changed")
    checked = confirmation.analyze(confirmation.RUN)
    if len(checked) != 10 or not all(r["passed"] for r in checked):
        raise ValueError("Recomputed independent-batch confirmation gate failed")
    cfg, old = scan.load_config(CONFIG), scan.load_config(SOURCE_CONFIG)
    physics = dict(cfg.raw["physics"])
    if physics.pop("model_id", None) != MODEL or physics != old.raw["physics"] or cfg.temperature_k != old.temperature_k:
        raise ValueError("Reused and fresh configurations do not share the same physical model")
    sr, jobs = configured_jobs(CONFIG)
    reused = completed_rows(SOURCE)[0]
    fresh, endpoints, rest = select_jobs(jobs, reused)
    paths = [CONFIG, Path(__file__), ROOT / "scripts/corrected_scan.sh",
             ROOT / "scripts/precision_analysis.py", ROOT / "scripts/staged_report.py",
             ROOT / "scripts/timing_pilot.py", ROOT / "scripts/preliminary_report.py",
             SOURCE_CONFIG, confirmation.RUN / "plan.json", confirmation.RUN / "completion.json",
             confirmation.RUN / "replay_gate.json", confirmation.RUN / "paired_statistics.json"]
    for r in reused:
        paths.extend((SOURCE / r["raw_archive"], SOURCE / r["normalized_profiles"],
                      SOURCE / "job_results/trim" / f"{r['job_id']}.json"))
    plan = dict(model_id=MODEL, input_sha256={str(p.relative_to(ROOT)): scan.sha256_file(p) for p in paths},
                inherited_sha256=frozen, expected_points=108, new_points=103, reused_points=5,
                new_batches=412, reused_batches=25, new_histories=20600, total_histories=21850,
                endpoint_ids=[j.job_id for j in endpoints], remaining_ids=[j.job_id for j in rest],
                reused_ids=[r["job_id"] for r in reused], reused_source=str(SOURCE),
                forecast=estimate(endpoints, rest, reused), budget_hours=8,
                endpoint_gate="All 72 endpoint batches complete with matching reported target energies, "
                              "valid raw artifacts/numerics, no clipping, no OOM, adequate RAM headroom and unchanged source.",
                precision="Fixed four-batch new cohorts (200 ions), five-batch reused cohorts (250 ions). "
                          "Estimated SEM targets: range 5%, ionization fraction 2%; flag failures, never auto-top-up.",
                exclusions="No epsilon_1e6, replay, five-ion pilot or legacy zero-input data; no pressure scaling/interpolation.")
    path = run / "plan.json"
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError("Frozen corrected-scan inputs changed")
    if not path.exists():
        write_json(path, plan)
    return plan, sr, fresh, endpoints, rest


def combined_rows(run):
    rows = []
    for source, reused in ((SOURCE, True), (run / "scan", False)):
        for row in completed_rows(source)[0]:
            check = row.get("target_energy_check", {})
            expected = scan.requested_target_energies(("C", "F", row["family"]), scan.load_config(CONFIG))
            if check.get("passed") is not True or check.get("expected") != expected or any(
                    check.get("observed", {}).get(s) != values for s, values in expected.items()):
                raise ValueError("Refusing mismatched/unchecked physics in corrected-model report")
            row.update(model_id=MODEL, reused=reused, source_run=str(source),
                       raw_archive=str((source / row["raw_archive"]).resolve()),
                       normalized_profiles=str((source / row["normalized_profiles"]).resolve()))
            rows.append(row)
    if len({r["job_id"] for r in rows}) != len(rows) or len({r["random_seed"] for r in rows}) != len(rows):
        raise ValueError("Duplicate history/job in combined dataset")
    if any(r["n_ions"] != 50 or (r["reused"] and analysis.key(r) not in confirmation.KEYS)
           or (not r["reused"] and analysis.key(r) in confirmation.KEYS) for r in rows):
        raise ValueError("Unexpected source or overlap in combined dataset")
    return rows


def publish(run, state, final=False, plots=False):
    rows = combined_rows(run)
    points = analysis.points(rows)
    for row in points:
        row["model_id"] = MODEL
        row["batches_expected"] = 5 if analysis.key(row) in confirmation.KEYS else 4
        row["all_batches_complete"] = row["batches_complete"] == row["batches_expected"]
    summary = dict(model_id=MODEL, expected_points=108, points_present=len(points),
                   points_complete=sum(p["all_batches_complete"] for p in points),
                   new_batches=sum(not r["reused"] for r in rows), expected_new_batches=412,
                   reused_batches=sum(r["reused"] for r in rows), total_histories=sum(r["n_ions"] for r in rows),
                   points_meeting_estimated_precision=sum(p["all_batches_complete"] and p["precision_met"] for p in points),
                   complete_points_needing_precision=sum(p["all_batches_complete"] and not p["precision_met"] for p in points))
    output = run / "report"
    output.mkdir(parents=True, exist_ok=True)
    flagged = [p for p in points if not p["precision_met"]]
    for name, data in (("batches", rows), ("points", points), ("precision_flagged", flagged)):
        atomic_text(output / f"{name}.csv", csv_text(data, sorted(set().union(*(r.keys() for r in data))) if data else ["no_rows"]))
    write_json(run / "science_status.json", summary)
    if plots or final:
        from matplotlib.backends.backend_pdf import PdfPages
        import matplotlib.pyplot as plt
        write_root(output / "scan.root", rows, points)
        with PdfPages(output / "summary.pdf") as pdf:
            for mixture in (60, 80, 95):
                fig = energy_figure(points, mixture, 1)
                fig.suptitle(f"Corrected 1e-9 eV model · {mixture}% noble · batch SEM, not model uncertainty")
                fig.savefig(output / f"energy_{mixture}.png", dpi=120)
                pdf.savefig(fig)
                plt.close(fig)
            fig = scan_figure(points, "noble_percent")
            fig.savefig(output / "mixtures.png", dpi=120)
            pdf.savefig(fig)
            plt.close(fig)
    body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Corrected SRIM mixture map</title><style>body{font:16px system-ui;max-width:1200px;margin:30px auto;padding:16px}
.scroll{overflow:auto;max-height:500px}td,th{padding:6px;border:1px solid #ddd}table{border-collapse:collapse}img{max-width:100%}</style>
<h1>Corrected SRIM Ar/Kr/Xe–CF4 mixture map</h1>
<p>Model noble-cf4-nearzero-1e9-v1: displacement 21 eV; lattice/surface 1e-9 eV.
60/80/95% noble, 1 atm, 293.15 K; C/F/noble recoils at 1/10/100/1000 keV.
108 direct points: 103 new points × 200 ions and five reused corrected-model points × 250 ions.</p>
<p>No legacy zero-input, epsilon_1e6, replay or five-ion pilot data are pooled here.
Only completed checkpointed batches appear. Four/five independent 50-ion batches per final point;
partial points are provisional. Targets: estimated range SEM ≤5%, ionization-fraction SEM ≤2%.
No adaptive extra batches. SEM does not measure model accuracy; SRIM ionization fraction is NOT detector-calibrated quenching.
There is no pressure comparison at this single pressure. Connecting plot lines are visual guides, not simulated intermediate values.</p>
<p>Limits: 15 cores/workers, RAM 24/32 GiB soft/hard, zero scan swap, eight cumulative hours.</p>
<p><a href="points.csv">Point results CSV</a> · <a href="batches.csv">Batch provenance/timings CSV</a> ·
<a href="precision_flagged.csv">Precision flags</a> · <a href="../endpoint_gate.json">Endpoint gate</a> ·
<a href="../plan.json">Frozen plan</a> · <a href="../logs/campaign.log">Logs</a></p>'''
    body += table([state], list(state)) + table([summary], list(summary))
    if (output / "summary.pdf").exists():
        body += '<p>Plots/export are phase snapshots; the CSV tables update throughout the run. <a href="summary.pdf">PDF</a> · <a href="scan.root">ROOT file</a></p>'
        body += ''.join(f'<img src="energy_{m}.png">' for m in (60,80,95)) + '<img src="mixtures.png">'
    body += table(points, ["family", "noble_percent", "ion", "energy_kev", "n_ions_total", "projected_range_mm",
                           "qf_trim_ionization", "projected_range_mm_relative_sem", "qf_trim_ionization_relative_sem", "precision_flag"])
    atomic_text(output / "index.html", body)
    return summary


class CorrectedCampaign(confirmation.ConfirmationCampaign):
    @property
    def budget_limit(self):
        return BUDGET

    def publish_report(self):
        return publish(self.run, self.state)

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            group = Path(resource_sample()["cgroup"])
            if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15
                    or (group / "memory.max").read_text().strip() != str(32*GIB)
                    or (group / "memory.swap.max").read_text().strip() != "0"):
                raise RuntimeError("Use corrected_scan.sh for enforced resource limits")
            if memory_info()["MemAvailable"] < 16*GIB or self.used >= BUDGET:
                raise RuntimeError("Insufficient host memory or cumulative time budget")
            plan, sr, jobs, endpoints, rest = prepare(self.run)
            source = scan.resolve_srim_root(None, scan.load_config(CONFIG))
            before = dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            original = json.loads((confirmation.pilot.RUN / "source_provenance.json").read_text())
            if original != dict(source=before, execution=scan.execution_provenance(scan.resolve_execution_context(scan.load_config(CONFIG)))):
                raise RuntimeError("Execution/source changed since binding validation")
            write_json(self.run / f"session-{os.getpid()}.json", dict(model_id=MODEL, source=before,
                       commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
                       cpu_affinity=sorted(os.sched_getaffinity(0)), started_at=self.state["started_at"]))
            old_jobs = [j for j in configured_jobs(SOURCE_CONFIG)[1] if job_key(j) in confirmation.KEYS]
            self.audit(SOURCE, old_jobs, SOURCE_CONFIG)
            self.state.update(status="RUNNING", phase="Accepted corrected-model reuse audited")
            self.update(force=True)
            destination = self.run / "scan"
            self.selected("27 corrected-model stopping tables", CONFIG, destination, "srmodule", sr)
            self.selected("36 endpoint points: two 50-ion batches each", CONFIG, destination, "trim", endpoints)
            self.audit(destination, endpoints, CONFIG)
            prepare(self.run)
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            unchanged = before == dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
            gate = dict(passed=unchanged and int(events.get("oom_kill",0))==0 and memory_info()["MemAvailable"]>=8*GIB,
                        expected_endpoint_batches=72, source_unchanged=unchanged, memory_events=events,
                        resources=resource_sample(), note="Parameter/artifact/resource gate, not a statistical precision gate.")
            write_json(self.run / "endpoint_gate.json", gate)
            if not gate["passed"]:
                raise RuntimeError("Endpoint resource/source gate failed; remainder not released")
            self.state["endpoint_gate_passed"] = True
            publish(self.run, self.state, plots=True)
            self.selected("Complete fixed corrected-model map: 340 remaining batches", CONFIG, destination, "trim", rest)
            self.audit(destination, jobs, CONFIG)
            self.audit(SOURCE, old_jobs, SOURCE_CONFIG)
            prepare(self.run)
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            if before != dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source)) or int(events.get("oom_kill",0)):
                raise RuntimeError("Source installation changed or OOM kill recorded")
            summary = publish(self.run, self.state)
            if summary["points_complete"] != 108 or summary["new_batches"] != 412 or summary["total_histories"] != 21850:
                raise RuntimeError("Final counts differ from frozen plan")
            self.state.update(status="REVIEW_REQUIRED", phase="Fixed corrected-model map complete", source_unchanged=True)
            self.update(force=True)
            summary = publish(self.run, self.state, final=True)
            write_json(self.run / "completion.json", dict(status="REVIEW_REQUIRED", summary=summary, resources=resource_sample(),
                       note="No further scan/top-up is released automatically; inspect statistical flags and timing first."))
            return 0
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping else "NEEDS_ATTENTION", error=str(exc))
            print(str(exc), file=sys.stderr, flush=True)
            self.update(force=True)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    subprocess.run(["systemctl","--user","--no-block","stop",UNIT],check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare","run","status"))
    args=parser.parse_args()
    if args.command=="status":
        unit=subprocess.run(["systemctl","--user","show",UNIT,"-p","ActiveState","-p","MemoryCurrent","-p","MemorySwapCurrent"],capture_output=True,text=True)
        print(unit.stdout.strip())
        if (RUN/"status.json").exists():
            state=json.loads((RUN/"status.json").read_text())
            print(json.dumps(state,indent=2))
            if state["status"] in ("RUNNING","STARTING") and ("ActiveState=active" not in unit.stdout or time.time()-datetime.fromisoformat(state["updated_at"]).timestamp()>300):
                print("WARNING: inactive service or stale heartbeat; inspect logs")
        print("New checkpoint:",completed_rows(RUN/"scan")[1])
        if (RUN/"science_status.json").exists():print((RUN/"science_status.json").read_text())
        print("Report:",RUN/"report/index.html")
        return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/"supervisor.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=="prepare":
            plan,*_=prepare(RUN)
            print(json.dumps({k:v for k,v in plan.items() if k not in ("input_sha256","inherited_sha256","endpoint_ids","remaining_ids","reused_ids")},indent=2))
            return 0
        return CorrectedCampaign(RUN).execute()


if __name__=="__main__":raise SystemExit(main())
