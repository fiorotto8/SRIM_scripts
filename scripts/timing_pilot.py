#!/usr/bin/env python3
"""Supervise the small timing pilot and publish live, checkpoint-backed results."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import statistics
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan
from scripts.preliminary_report import atomic_text, csv_text, table

CONFIG = ROOT / "configs/noble_cf4_timing_pilot.toml"
RUN = ROOT / "runs/noble_cf4_timing_pilot"
GIB = 1024 ** 3


def memory_info():
    return {key: int(value.split()[0]) * 1024 for key, value in
            (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}


def resource_sample():
    membership = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    group = Path("/sys/fs/cgroup") / membership.lstrip("/")
    memory = memory_info()
    result = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
              "host_available_gib": memory["MemAvailable"] / GIB,
              "host_swap_used_gib": (memory["SwapTotal"] - memory["SwapFree"]) / GIB}
    for field in ("memory.current", "memory.peak", "memory.swap.current"):
        result[field.replace(".", "_") + "_gib"] = int((group / field).read_text()) / GIB
    cpu = dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())
    result["cgroup_cpu_seconds"] = int(cpu["usage_usec"]) / 1e6
    result["cgroup"] = str(group)
    return result


def completed_rows(run):
    if not (run / "jobs.sqlite").exists():
        return [], []
    connection = sqlite3.connect((run / "jobs.sqlite").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        counts = connection.execute("SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status").fetchall()
        records = connection.execute("SELECT result_path,started_at,finished_at,attempts FROM jobs WHERE stage='trim' AND status='complete'").fetchall()
    finally:
        connection.close()
    rows = []
    for relative, started, finished, attempts in records:
        row = json.loads((run / relative).read_text())
        row["wall_seconds_last_attempt"] = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
        row["wall_seconds_per_ion"] = row["wall_seconds_last_attempt"] / row["n_ions"]
        row["attempts"] = attempts
        with tarfile.open(run / row["raw_archive"], "r:gz") as archive:
            try:
                progress = json.load(archive.extractfile(".trim-progress.json"))
            except KeyError:
                progress = {}
        row["trim_cpu_seconds_observed"] = progress.get("cpu_seconds", "")
        row["trim_peak_rss_mib_observed"] = progress.get("peak_rss_bytes_observed", 0) / 1024**2
        rows.append(row)
    rows.sort(key=lambda r: (r["energy_kev"], r["family"], r["noble_percent"], r["ion"]))
    return rows, counts


def report(run, state):
    rows, counts = completed_rows(run)
    fields = ["job_id", "family", "noble_percent", "pressure_atm", "ion", "energy_kev", "n_ions",
              "projected_range_mm", "longitudinal_straggling_mm", "qf_trim_ionization", "clipped", "warnings",
              "wall_seconds_last_attempt", "wall_seconds_per_ion", "trim_cpu_seconds_observed",
              "trim_peak_rss_mib_observed", "attempts", "raw_archive"]
    atomic_text(run / "timing_results.csv", csv_text(rows, fields))
    groups = []
    for energy in (1, 10, 100, 1000):
        group = [r for r in rows if abs(r["energy_kev"] - energy) < .001]
        times = [r["wall_seconds_last_attempt"] for r in group]
        groups.append(dict(energy_kev=energy, completed=len(group), planned=12,
                           median_job_seconds=statistics.median(times) if times else "pending",
                           slowest_job_seconds=max(times) if times else "pending"))
    metrics = state.get("resources", {})
    updated = datetime.now().astimezone().isoformat(timespec="seconds")
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30">
<title>SRIM 1 keV–1 MeV timing pilot</title><style>
body{{font:16px system-ui;max-width:1250px;margin:30px auto;padding:0 16px;color:#192535}}
table{{border-collapse:collapse;font-size:13px}}td,th{{padding:7px;border-bottom:1px solid #ddd;white-space:nowrap;text-align:left}}
th{{background:#edf3fa;position:sticky;top:0}}.scroll{{overflow:auto;max-height:500px}}.notice{{background:#fff1cf;padding:16px}}
</style><h1>SRIM timing pilot: 1 keV–1 MeV</h1><p>Updated {updated} · {state['status']} · {len(rows)}/48 TRIM points complete</p>
<p class="notice"><b>Timing test: only 5 ions per point.</b> Range and ionization results are extremely low-statistics;
they are not converged quenching measurements. This pilot measures runtime and memory before scaling up.</p>
<p>Ar and Xe + CF4 · 60% and 95% noble gas · 1 atm · C, F and noble recoils · 1, 10, 100, 1,000 keV.</p>
<p>At most 15 workers and 15 allowed CPU cores; RAM soft/hard limits 24/32 GiB; scan swap limit 0.</p>
<p><a href="timing_results.csv">Download results and per-point timings</a> ·
<a href="resource_samples.csv">RAM/CPU measurements</a> · <a href="logs/pilot.log">Run log</a></p>
<h2>Runtime by energy</h2>{table(groups, list(groups[0]))}
<p>Job time includes Wine startup, simulation, any target enlargement, parsing and archiving. Per-ion time is descriptive,
not an exact linear scaling model. CPU and per-process peak RSS are sampled from TRIM; cgroup memory includes all workers,
Wine, Xvfb, Python and charged file cache. Failed attempts are not hidden by these timings: see attempts and the run log.</p>
<h2>Resources (latest sample)</h2>{table([metrics], list(metrics)) if metrics else '<p>Waiting for measurements.</p>'}
<p>Total supervisor elapsed time: {state.get('elapsed_seconds', 0):.1f} seconds.</p>
<h2>Checkpoint</h2>{table([dict(stage=s,status=t,jobs=n) for s,t,n in counts], ['stage','status','jobs'])}
<h2>Completed results</h2>{table(rows, fields[1:-1])}
<p>Ionization fraction = integrated primary-plus-secondary TRIM ionization energy / initial recoil energy.
It is not a calibrated detector response. No interpolation or extra pressure points are represented as simulations.</p></html>"""
    atomic_text(run / "report.html", content)
    atomic_text(run / "pilot_status.json", json.dumps(state, indent=2) + "\n")


def supervise(run):
    if len(os.sched_getaffinity(0)) > 15:
        raise RuntimeError("Use start_timing_pilot.sh: CPU affinity must be limited to at most 15 cores.")
    sample = resource_sample()
    group = Path(sample["cgroup"])
    if int((group / "memory.max").read_text()) > 32 * GIB or (group / "memory.swap.max").read_text().strip() != "0":
        raise RuntimeError("Use the resource-limited systemd launcher (32 GiB maximum, no scan swap).")
    if memory_info()["MemAvailable"] < 16 * GIB:
        raise RuntimeError("Less than 16 GiB RAM is available; defer commissioning until headroom improves.")
    old = ROOT / "runs/noble_cf4_preliminary/jobs.sqlite"
    if old.exists():
        connection = sqlite3.connect(old.as_uri() + "?mode=ro", uri=True)
        try:
            unresolved = connection.execute("SELECT COUNT(*) FROM jobs WHERE status!='complete' AND error LIKE '%TRIM_WATCHDOG_ERROR%'").fetchone()[0]
        finally:
            connection.close()
        if unresolved:
            raise RuntimeError("First recover the previous run's rejected watchdog job; it is still pending.")
    run.mkdir(parents=True, exist_ok=True)
    (run / "logs").mkdir(exist_ok=True)
    started = time.monotonic()
    state = dict(status="STARTING", started_at=datetime.now().astimezone().isoformat(), resources=sample)
    command = [str(ROOT / "scripts/run_ubuntu_7day.sh"), str(run), "--config", str(CONFIG),
               "--workers", str(min(15, len(os.sched_getaffinity(0))))]
    atomic_text(run / "supervisor.pid", str(os.getpid()) + "\n")
    stop_requested = False
    child = None

    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True
        if child is not None and child.poll() is None:
            child.terminate()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        with (run / "logs/pilot.log").open("a") as log, (run / "resource_samples.csv").open("a", newline="") as samples:
            writer = csv.DictWriter(samples, fieldnames=list(sample))
            if samples.tell() == 0:
                writer.writeheader()
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            last_report = -60.0
            while True:
                code = child.poll()
                state.update(resources=resource_sample(), elapsed_seconds=time.monotonic() - started,
                             status="RUNNING" if code is None else f"STOPPED (exit {code})")
                writer.writerow(state["resources"])
                samples.flush()
                if code is not None or time.monotonic() - last_report >= 30:
                    rows, counts = completed_rows(run)
                    if code == 0 and len(rows) == 48:
                        state["status"] = "FINISHED — all 48 points complete"
                    report(run, state)
                    last_report = time.monotonic()
                if code is not None:
                    if code == 0:
                        rows, counts = completed_rows(run)
                        errors = []
                        for row in rows:
                            issue = srim_scan.validate_raw_archive(run / row["raw_archive"], row["job_id"], "trim",
                                                                  ("TRIM.IN", "TRIMAUTO", *srim_scan.STANDARD_TRIM_OUTPUTS))
                            if issue:
                                errors.append([row["job_id"], issue])
                            if (row["clipped"] or not 0 <= row["qf_trim_ionization"] <= 1.000001
                                    or not math.isfinite(row["projected_range_mm"]) or row["projected_range_mm"] <= 0):
                                errors.append([row["job_id"], "numeric bounds or clipping"])
                        atomic_text(run / "quick_validation.json", json.dumps(dict(completed=len(rows), issues=errors), indent=2))
                        if errors:
                            state["status"] = "FINISHED WITH VALIDATION ISSUES — see quick_validation.json"
                            report(run, state)
                            return 1
                    return code if code else (130 if stop_requested else 0)
                time.sleep(5)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                # The systemd unit owns the entire descendant cgroup and applies
                # its stop timeout. Do not leave an unmonitored run on an exception.
                subprocess.run(["systemctl", "--user", "--no-block", "stop", "srim-timing-pilot.service"], check=False)
        (run / "supervisor.pid").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=RUN)
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    if args.report_only:
        state = json.loads((run / "pilot_status.json").read_text())
        report(run, state)
        return 0
    run.mkdir(parents=True, exist_ok=True)
    with (run / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return supervise(run)


if __name__ == "__main__":
    raise SystemExit(main())
