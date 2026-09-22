#!/usr/bin/env python3
"""Bounded 1-atm mixture refinement with a fixed independent control cohort."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime
import fcntl
import hashlib
import heapq
import importlib.metadata
import json
import math
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
from scripts import low_energy_topup as topup
from scripts import precision_analysis as analysis
from scripts.precision_scan import job_key, pending
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_report import write_root
from scripts.staged_scan import configured_jobs, validate_selected, write_json
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / "runs/noble_cf4_mixture_refinement"
CONFIG = ROOT / "configs/noble_cf4_mixture_refinement.toml"
UNIT = "srim-mixture-scan.service"
BUDGET = 6 * 3600
FRACTIONS = (65, 75, 85)
ENERGIES = (1, 3, 10, 30, 100)
# Fixed before observing the new cohort. Each curve has three independent points.
CONTROL_CURVES = (
    ("Kr", "C", 30., (70, 80, 90)),
    ("Xe", "Xe", 30., (80, 90, 95)),
    ("Kr", "Kr", 100., (80, 90, 95)),
    ("Ar", "Ar", 100., (60, 70, 80)),
)
CONTROL_KEYS = {(family, fraction, 1., ion, energy)
                for family, ion, energy, fractions in CONTROL_CURVES for fraction in fractions}
MIXTURE_KEYS = {(family, fraction, 1., ion, float(energy))
                for family in ("Ar", "Kr", "Xe") for fraction in FRACTIONS
                for ion in (family, "C", "F") for energy in ENERGIES}


def prior_rows():
    return topup.prior_rows() + topup.read_new(topup.RUN)


def select_jobs(rows, jobs):
    old_keys = {analysis.key(r) for r in rows}
    if not CONTROL_KEYS <= old_keys or MIXTURE_KEYS & old_keys:
        raise ValueError("Expected existing controls and genuinely new mixture points")
    chosen = [j for j in jobs if job_key(j) in CONTROL_KEYS | MIXTURE_KEYS]
    groups = defaultdict(list)
    seeds = defaultdict(set)
    ids = {r["job_id"] for r in rows}
    for row in rows:
        seeds[analysis.key(row)].add(row["random_seed"])
    for job in chosen:
        key = job_key(job)
        seed = srim_scan.seed_for(job.job_id, job.payload["repeat"])
        if job.job_id in ids or seed in seeds[key]:
            raise ValueError("Job or per-point random seed would be reused")
        seeds[key].add(seed)
        groups[key].append(job)
    if set(groups) != CONTROL_KEYS | MIXTURE_KEYS or any(
            len(batch) != 8 or {j.payload["repeat"] for j in batch} != set(range(30, 38)) or
            any(j.payload["n_ions"] != 50 for j in batch) for batch in groups.values()):
        raise ValueError("Expected eight fresh 50-ion batches at all 147 selected points")
    return [j for j in chosen if job_key(j) in CONTROL_KEYS], [j for j in chosen if job_key(j) in MIXTURE_KEYS]


def cost_model(rows):
    times = defaultdict(list)
    for row in rows:
        times[analysis.key(row)].append(row["wall_seconds_last_attempt"])
    def cost(job):
        k = job_key(job)
        if k in times:
            return statistics.mean(times[k])
        family, fraction, pressure, ion, energy = k
        return statistics.mean(statistics.mean(times[(family, neighbor, pressure, ion, energy)])
                               for neighbor in (fraction - 5, fraction + 5))
    return cost


def forecast(jobs, cost):
    workers = [0.] * 15
    for value in sorted((cost(j) for j in jobs), reverse=True):
        heapq.heapreplace(workers, workers[0] + value)
    return max(workers)


def prepare(run):
    sources = [CONFIG, topup.BASELINE / "trim_results.csv", topup.PREVIOUS / "refinement/trim_results.csv",
               topup.RUN / "refinement/trim_results.csv", topup.RUN / "completion.json"]
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    completion = json.loads((topup.RUN / "completion.json").read_text())
    if completion["status"] != "TARGETS_MET" or completion["summary"]["points_present"] != 1890:
        raise ValueError("Expected the completed 1890-point precision campaign")
    rows = prior_rows()
    if len(analysis.points(rows)) != 1890:
        raise ValueError("Previous physical-point grid changed")
    sr, all_jobs = configured_jobs(CONFIG)
    controls, mixture = select_jobs(rows, all_jobs)
    pairs = {(j.payload["condition"]["condition_id"], j.payload["ion"]) for j in controls + mixture}
    stopping = [j for j in sr if (j.payload["condition"]["condition_id"], j.payload["ion"]) in pairs]
    cost = cost_model(rows)
    control_seconds, mixture_seconds = forecast(controls, cost), forecast(mixture, cost)
    plan = dict(input_hashes=hashes, control_job_ids=[j.job_id for j in controls],
                mixture_job_ids=[j.job_id for j in mixture], srmodule_job_ids=[j.job_id for j in stopping],
                control_points=12, new_mixture_points=135, expected_combined_points=2025,
                expected_new_batches=1176, expected_new_ions=58800, ions_per_selected_point=400,
                controls_model_hours=control_seconds / 3600, mixtures_model_hours=mixture_seconds / 3600,
                planning_hours=1.5 * (control_seconds + mixture_seconds) / 3600 + .25,
                cumulative_budget_hours=6, control_curves=CONTROL_CURVES,
                assumptions="15-worker scheduling; same-ion/energy/pressure neighboring-mixture mean cost; 50% margin plus 15 minutes; fixed selection; no extrapolated pressure rows")
    # Round-trip tuple fields before comparison with a frozen JSON manifest.
    plan = json.loads(json.dumps(plan))
    path = run / "plan.json"
    if path.exists():
        if json.loads(path.read_text()) != plan:
            raise ValueError("Frozen plan or inputs changed; do not resume this run")
    else:
        write_json(path, plan)
    return plan, stopping, controls, mixture, cost


def control_contrasts(rows, cohort, minimum_batches=8):
    points = {analysis.key(r): r for r in analysis.points(rows)}
    result = []
    for family, ion, energy, fractions in CONTROL_CURVES:
        keys = [(family, fraction, 1., ion, energy) for fraction in fractions]
        if not all(k in points and points[k]["batches_complete"] >= minimum_batches for k in keys):
            continue
        a, b, c = [points[k] for k in keys]
        weight = (fractions[1] - fractions[0]) / (fractions[2] - fractions[0])
        for field in analysis.FIELDS:
            interpolated = (1 - weight) * a[field] + weight * c[field]
            deviation = b[field] - interpolated
            sem = math.sqrt(b[field + "_batch_sem"]**2 +
                            (1 - weight)**2 * a[field + "_batch_sem"]**2 + weight**2 * c[field + "_batch_sem"]**2)
            result.append(dict(cohort=cohort, family=family, ion=ion, energy_kev=energy,
                               noble_percent_low=fractions[0], noble_percent_middle=fractions[1], noble_percent_high=fractions[2],
                               quantity=field, deviation=deviation, deviation_relative=deviation / interpolated,
                               deviation_sem=sem, descriptive_sem_units=deviation / sem if sem else None))
    return result


def mixture_figure(points, recoil):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for col, family in enumerate(("Ar", "Kr", "Xe")):
        ion = family if recoil == "noble" else recoil
        for energy in ENERGIES:
            selected = sorted((r for r in points if r["family"] == family and r["ion"] == ion and
                               r["pressure_atm"] == 1 and r["energy_kev"] == energy), key=lambda r: r["noble_percent"])
            for row, field in enumerate(analysis.FIELDS):
                axes[row, col].errorbar([r["noble_percent"] for r in selected], [r[field] for r in selected],
                                      yerr=[r[field + "_batch_sem"] or 0 for r in selected], marker="o", capsize=3,
                                      label=f"{energy:g} keV")
                axes[row, col].grid(alpha=.25)
        axes[0, col].set_title(f"{family}–CF4 · {ion} recoils · 1 atm")
        axes[0, col].set_yscale("log")
        axes[1, col].set_xlabel("Noble-gas mole fraction [%]")
        axes[1, col].legend(fontsize=8)
    axes[0, 0].set_ylabel("Projected range [mm]")
    axes[1, 0].set_ylabel("SRIM ionization fraction")
    fig.suptitle("Mixture refinement · batch SEM, not model accuracy · lines guide the eye")
    return fig


def publish(run, state, final=False):
    old, new = prior_rows(), topup.read_new(run)
    combined = analysis.points(old + new)
    fresh = analysis.points(new)
    controls = [r for r in fresh if analysis.key(r) in CONTROL_KEYS]
    mixtures = [r for r in fresh if analysis.key(r) in MIXTURE_KEYS]
    unresolved = [r for r in combined if not r["precision_met"]]
    contrasts = control_contrasts(old, "previous_selected_data", 4) + control_contrasts(new, "independent_400_ions")
    summary = dict(expected_combined_points=2025, points_present=len(combined),
                   completed_new_batches=len(new), expected_new_batches=1176,
                   mixture_points_complete=sum(r["n_ions_total"] == 400 for r in mixtures), expected_mixture_points=135,
                   independent_controls_complete=sum(r["n_ions_total"] == 400 for r in controls), expected_control_points=12,
                   mixture_points_meeting_targets=sum(r["n_ions_total"] == 400 and r["precision_met"] for r in mixtures),
                   independent_controls_meeting_targets=sum(r["n_ions_total"] == 400 and r["precision_met"] for r in controls),
                   combined_unresolved_precision_points=len(unresolved))
    output = run / "report"
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("points", combined), ("new_batches", new), ("new_mixture_points", mixtures),
                       ("independent_controls", controls), ("control_curvature", contrasts), ("precision_flagged", unresolved)):
        atomic_text(output / f"{name}.csv", csv_text(data, list(data[0]) if data else ["no_rows"]))
    write_json(run / "science_status.json", summary)
    if final:
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_pdf import PdfPages
        write_root(output / "scan.root", old + new, combined)
        with PdfPages(output / "mixture_summary.pdf") as pdf:
            for recoil in ("noble", "C", "F"):
                fig = mixture_figure(combined, recoil)
                fig.savefig(output / f"mixture_{recoil}.png", dpi=130)
                pdf.savefig(fig)
                plt.close(fig)
    body = f'''<!doctype html><html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>SRIM mixture refinement</title><style>body{{font:16px system-ui;max-width:1250px;margin:25px auto}}.scroll{{overflow:auto;max-height:500px}}td,th{{padding:6px;border-bottom:1px solid #ddd}}img{{max-width:100%}}</style>
<h1>Ar/Kr/Xe–CF4 mixture refinement</h1>
<p>135 new points: 65/75/85% noble gas, 1 atm, C/F/noble recoils, 1/3/10/30/100 keV.
Eight 50-ion batches per point. Twelve independent control points (400 new ions each) run first.
All earlier datasets are read-only. No automatic high-energy, pressure or history-count expansion.</p>
<p>Limits: 15 cores/workers, 32 GiB hard RAM, zero scan swap, six cumulative hours.
Estimated SEM targets: range ≤5%, ionization fraction ≤2%. Statistical precision is not detector quenching or model accuracy.</p>
<p>Curvature is the middle concentration minus linear interpolation of its two neighbors.
The independent-control table uses only fresh batches. Contrasts and propagated SEM units are descriptive,
not multiple-testing-corrected significance. Previous selected data and pooled intervals are not independent confirmation.
Pressure hypotheses are not retested by this single-pressure scan.</p>
<p><a href="points.csv">Combined CSV</a> · <a href="new_mixture_points.csv">New mixtures</a> ·
<a href="independent_controls.csv">Independent controls</a> · <a href="control_curvature.csv">Control curvature</a> ·
<a href="precision_flagged.csv">Unresolved precision</a> · <a href="new_batches.csv">New batches</a> ·
<a href="../plan.json">Frozen plan</a> · <a href="../../noble_cf4_low_energy_topup/report/index.html">Previous report</a></p>
{table([state], list(state))}{table([summary], list(summary))}
<h2>Control comparisons</h2>{table(contrasts, ['cohort','family','ion','energy_kev','noble_percent_middle','quantity','deviation_relative','descriptive_sem_units'])}
<h2>Remaining precision issues</h2>{table(unresolved, ['family','noble_percent','pressure_atm','ion','energy_kev','n_ions_total','projected_range_mm_relative_sem','qf_trim_ionization_relative_sem'])}'''
    if final:
        body += '<p><a href="mixture_summary.pdf">Mixture plots PDF</a> · <a href="scan.root">Combined ROOT file</a></p>'
        body += ''.join(f'<img src="mixture_{recoil}.png">' for recoil in ("noble", "C", "F"))
    atomic_text(output / "index.html", body + "</html>")
    return summary


class MixtureCampaign(topup.TopupCampaign):
    @property
    def budget_limit(self):
        return BUDGET

    @property
    def config_path(self):
        return CONFIG

    def publish_report(self):
        return publish(self.run, self.state)

    def gate_budget(self, jobs, cost, correction=1.):
        todo = pending(self.run / "refinement", jobs)
        self.update()
        if 1.5 * correction * forecast(todo, cost) + 15 * 60 > BUDGET - self.used:
            raise RuntimeError("Remaining work plus margin exceeds the six-hour cumulative budget")

    def validate_phase(self, jobs, name):
        issues = validate_selected(self.run / "refinement", jobs)
        write_json(self.run / f"{name}_validation.json", dict(expected_batches=len(jobs), issues=issues))
        if issues:
            raise RuntimeError(f"{name} failed validation; no subsequent phase released")

    def execute(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.stop)
        try:
            group = Path(resource_sample()["cgroup"])
            if group.name != UNIT or len(os.sched_getaffinity(0)) > 15:
                raise RuntimeError("Launch with mixture_scan.sh for enforced resource limits")
            if ((group / "memory.max").read_text().strip() != str(32 * GIB) or
                    (group / "memory.swap.max").read_text().strip() != "0"):
                raise RuntimeError("Expected 32 GiB RAM and zero scan swap")
            if memory_info()["MemAvailable"] < 16 * GIB or self.used >= BUDGET:
                raise RuntimeError("Insufficient host RAM or cumulative time budget")
            plan, sr, controls, mixture, cost = prepare(self.run)
            write_json(self.run / f"session-{os.getpid()}.json", dict(
                commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                started_at=self.state["started_at"], cpu_affinity=sorted(os.sched_getaffinity(0)),
                packages={p: importlib.metadata.version(p) for p in ("numpy", "scipy", "uproot", "matplotlib")}))
            self.state.update(status="RUNNING", phase="Read-only audit of previous controls")
            self.update(force=True)
            for source, config in ((topup.BASELINE, ROOT / "configs/noble_cf4_production.toml"),
                                   (topup.PREVIOUS / "refinement", ROOT / "configs/noble_cf4_precision.toml")):
                existing, _ = completed_rows(source)
                ids = {r["job_id"] for r in existing if analysis.key(r) in CONTROL_KEYS}
                _, candidates = configured_jobs(config)
                checked = [j for j in candidates if j.job_id in ids]
                issues = validate_selected(source, checked)
                if len(checked) != len(ids) or issues:
                    raise RuntimeError(f"Previous control artifact audit failed for {source}: {issues[:5]}")
            self.gate_budget(controls + mixture, cost)
            self.selected_phase("Selected stopping tables", sr, "srmodule")
            self.selected_phase("Independent controls: 12 points, 400 ions each", controls, "trim")
            self.validate_phase(controls, "controls")
            observed, _ = completed_rows(self.run / "refinement")
            by_id = {j.job_id: j for j in controls}
            correction = max(1., statistics.median(r["wall_seconds_last_attempt"] / cost(by_id[r["job_id"]])
                             for r in observed if r["job_id"] in by_id))
            events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
            if int(events.get("oom_kill", 0)) or memory_info()["MemAvailable"] < 8 * GIB:
                raise RuntimeError("Post-control resource gate failed")
            write_json(self.run / "control_gate.json", dict(validated=True, observed_median_timing_correction=correction,
                       memory_events=events, note="Timing calibration only; statistical/curvature findings remain for review"))
            self.gate_budget(mixture, cost, correction)
            self.selected_phase("New 65/75/85% mixtures: 135 points, 400 ions each", mixture, "trim")
            self.validate_phase(controls + mixture, "final")
            prepare(self.run)
            summary = publish(self.run, self.state)
            if (summary["points_present"] != 2025 or summary["completed_new_batches"] != 1176 or
                    summary["mixture_points_complete"] != 135 or summary["independent_controls_complete"] != 12):
                raise RuntimeError("Final coverage differs from the frozen plan")
            review = summary["combined_unresolved_precision_points"] or summary["independent_controls_meeting_targets"] < 12
            self.state.update(status="REVIEW_REQUIRED" if review else "TARGETS_MET", phase="Fixed mixture scan complete")
            self.update(force=True)
            summary = publish(self.run, self.state, final=True)
            write_json(self.run / "completion.json", dict(status=self.state["status"], summary=summary,
                       note="All scheduled batches validated. Curvature and next energy/pressure steps require review; no automatic expansion."))
            return 0
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping else "NEEDS_ATTENTION", error=str(exc))
            print(f"Mixture campaign stopped: {exc}", file=sys.stderr, flush=True)
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
            if state["status"] == "RUNNING" and ("ActiveState=active" not in unit.stdout or time.time() - datetime.fromisoformat(state["updated_at"]).timestamp() > 600):
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
            plan, _, _, _, _ = prepare(RUN)
            print(json.dumps({k: v for k, v in plan.items() if not k.endswith("job_ids") and k != "input_hashes"}, indent=2))
            return 0
        return MixtureCampaign(RUN).execute()


if __name__ == "__main__":
    raise SystemExit(main())
