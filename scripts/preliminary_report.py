#!/usr/bin/env python3
"""Read-only checkpoint reader; publish an atomic, self-contained quick-look report."""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime
import html
import io
import json
import os
from pathlib import Path
import sqlite3
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ["source_run", "job_id", "family", "noble_percent", "cf4_percent", "pressure_atm",
          "ion", "energy_kev", "n_ions", "projected_range_mm", "longitudinal_straggling_mm",
          "qf_trim_ionization", "clipped", "warnings", "raw_archive"]


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def checkpoint(run_dir):
    """Only expose results committed complete, never running/partial worker output."""
    database = run_dir / "jobs.sqlite"
    if not database.exists():
        return [], []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        counts = connection.execute("SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status").fetchall()
        records = connection.execute(
            "SELECT result_path FROM jobs WHERE stage='trim' AND status='complete' ORDER BY job_id"
        ).fetchall()
    finally:
        connection.close()
    rows = []
    for (relative,) in records:
        row = json.loads((run_dir / relative).read_text())
        row["source_run"] = run_dir.name
        rows.append(row)
    rows.sort(key=lambda row: tuple(row[key] for key in
              ("family", "noble_percent", "pressure_atm", "ion", "energy_kev")))
    return rows, counts


def csv_text(rows, fields):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def embedded_figure(fig, output):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    # PNG is also available separately for sharing.
    temporary = output.with_name(output.name + f".tmp-{os.getpid()}")
    temporary.write_bytes(buffer.getvalue())
    temporary.replace(output)
    return '<img alt="SRIM preliminary plots" src="data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode() + '">'


def srmodule_plot(rows, output):
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for column, family in enumerate(("Ar", "Kr", "Xe")):
        for ion in ("C", "F", family):
            selected = sorted((r for r in rows if r["family"] == family and r["ion"] == ion
                               and int(r["noble_percent"]) == 80 and float(r["pressure_atm"]) == 1.0),
                              key=lambda r: float(r["energy_kev"]))
            x = [float(r["energy_kev"]) for r in selected]
            axes[0, column].loglog(x, [float(r["projected_range_mm"]) for r in selected], label=ion)
            axes[1, column].semilogx(x, [float(r["srmodule_electronic_fraction_proxy"]) for r in selected], label=ion)
        axes[0, column].set_title(f"{family}/CF4 80/20, 1 atm")
        for row in range(2):
            axes[row, column].grid(alpha=.25)
            axes[row, column].legend()
        axes[1, column].set_xlabel("Recoil energy [keV]")
        axes[1, column].set_ylim(0, 1)
    axes[0, 0].set_ylabel("SRModule projected range [mm]")
    axes[1, 0].set_ylabel("Electronic stopping proxy (NOT TRIM QF)")
    return embedded_figure(fig, output)


def trim_plot(rows, output):
    if not rows:
        return "<p>Waiting for the first completed TRIM points.</p>"
    # Show the best-covered mixture, so useful curves appear early.
    mixture = max((60, 80, 95), key=lambda n: sum(int(r["noble_percent"]) == n for r in rows))
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for column, family in enumerate(("Ar", "Kr", "Xe")):
        for ion, color in zip(("C", "F", family), ("tab:blue", "tab:orange", "tab:green")):
            for pressure, style in ((1.0, "-"), (3.0, "--")):
                selected = sorted((r for r in rows if r["family"] == family and r["ion"] == ion
                                   and int(r["noble_percent"]) == mixture and float(r["pressure_atm"]) == pressure),
                                  key=lambda r: float(r["energy_kev"]))
                if not selected:
                    continue
                x = [float(r["energy_kev"]) for r in selected]
                for row, field in enumerate(("projected_range_mm", "qf_trim_ionization")):
                    axes[row, column].plot(x, [float(r[field]) for r in selected],
                                           style, marker="o", color=color, label=f"{ion}, {pressure:g} atm")
        axes[0, column].set_title(f"{family}/CF4 {mixture}/{100-mixture}, 20 ions/point")
        for row in range(2):
            axes[row, column].set_xscale("log")
            axes[row, column].grid(alpha=.25)
            if axes[row, column].lines:
                axes[row, column].legend(fontsize=8)
        axes[0, column].set_yscale("log")
        axes[1, column].set_xlabel("Recoil energy [keV]")
    axes[0, 0].set_ylabel("TRIM projected range [mm]")
    axes[1, 0].set_ylabel("TRIM ionization energy fraction")
    fig.suptitle("LOW STATISTICS — no convergence test or uncertainty estimate")
    return embedded_figure(fig, output)


def table(rows, fields):
    def cell(value):
        if isinstance(value, float):
            value = f"{value:.5g}"
        return html.escape(str(value))
    return ("<div class='scroll'><table><thead><tr>" + "".join(f"<th>{cell(k)}</th>" for k in fields)
            + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{cell(r.get(k, ''))}</td>" for k in fields)
                                              + "</tr>" for r in rows) + "</tbody></table></div>")


def mixture_plot(rows, output):
    selected = [r for r in rows if r["ion"] == r["family"] and 7 < float(r["energy_kev"]) < 8]
    if not selected:
        return "<p>Mixture comparison appears when the 7.071 keV noble-recoil points finish.</p>"
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for column, family in enumerate(("Ar", "Kr", "Xe")):
        for pressure, style in ((1.0, "-o"), (3.0, "--s")):
            group = sorted((r for r in selected if r["family"] == family and float(r["pressure_atm"]) == pressure),
                           key=lambda r: int(r["noble_percent"]))
            if not group:
                continue
            x = [int(r["noble_percent"]) for r in group]
            axes[0, column].plot(x, [1000 * float(r["projected_range_mm"]) for r in group], style, label=f"{pressure:g} atm")
            axes[1, column].plot(x, [float(r["qf_trim_ionization"]) for r in group], style, label=f"{pressure:g} atm")
        axes[0, column].set_title(f"{family} recoils in {family}/CF4")
        axes[1, column].set_xlabel("Noble-gas mole fraction [%]")
        for row in range(2):
            axes[row, column].set_xticks([60, 80, 95])
            axes[row, column].grid(alpha=.25)
            if axes[row, column].lines:
                axes[row, column].legend()
    axes[0, 0].set_ylabel("TRIM projected range [µm]")
    axes[1, 0].set_ylabel("TRIM ionization energy fraction")
    fig.suptitle("Mixture and pressure comparison at 7.071 keV — 20 ions/point, LOW STATISTICS")
    return embedded_figure(fig, output)


def generate(run_dir, reference_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    rows, counts = checkpoint(run_dir)
    previous, _ = checkpoint(reference_dir)
    atomic_text(run_dir / "preliminary_trim.csv", csv_text(rows, FIELDS))
    atomic_text(run_dir / "previous_trim_1000ions.csv", csv_text(previous, FIELDS))
    sr_path = reference_dir / "srmodule_results.csv"
    if not sr_path.exists():
        sr_path = run_dir / "srmodule_results.csv"
    sr_rows = []
    if sr_path.exists():
        with sr_path.open(newline="") as handle:
            sr_rows = list(csv.DictReader(handle))
    source = sr_path.parent.name
    sr_fields = ["source_run", "family", "noble_percent", "cf4_percent", "pressure_atm", "ion",
                 "energy_kev", "projected_range_mm", "longitudinal_straggling_mm", "lateral_straggling_mm",
                 "srmodule_electronic_fraction_proxy"]
    atomic_text(run_dir / "srmodule_quicklook.csv", csv_text(({**r, "source_run": source} for r in sr_rows), sr_fields))
    sr_image = srmodule_plot(sr_rows, run_dir / "srmodule_overview.png") if sr_rows else "<p>SRModule results pending.</p>"
    trim_image = trim_plot(rows, run_dir / "trim_overview.png")
    mixture_image = mixture_plot(rows, run_dir / "mixture_pressure.png")
    count_rows = [dict(stage=s, status=t, jobs=n) for s, t, n in counts]
    coverage = [dict(family=f, completed=sum(r["family"] == f for r in rows), planned=90) for f in ("Ar", "Kr", "Xe")]
    updated = datetime.now().astimezone().isoformat(timespec="seconds")
    pid_file = run_dir / "campaign.pid"
    running = False
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text().strip()), 0)
            running = True
        except (ValueError, ProcessLookupError):
            pass
    state = "RUNNING" if running else ("FINISHED" if len(rows) == 270 else "STOPPED / NOT STARTED")
    content = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>SRIM preliminary results</title><style>
body{{font:16px system-ui,sans-serif;max-width:1250px;margin:30px auto;padding:0 18px;color:#192535}}
img{{width:100%;height:auto}} table{{border-collapse:collapse;font-size:13px}} td,th{{padding:7px;border-bottom:1px solid #ddd;text-align:left;white-space:nowrap}}
th{{background:#edf3fa;position:sticky;top:0}} .scroll{{overflow:auto;max-height:440px}} .notice{{background:#fff1cf;padding:18px;border-radius:8px}} a{{color:#0759a8}}
</style><h1>SRIM: preliminary results</h1>
<p>Updated {updated} · {state} · <b>{len(rows)}/270 new TRIM points complete</b></p>
<p class="notice">New scan: <b>20 ions per point — LOW STATISTICS</b>. No convergence test or error bars yet.
These are simulation quick-look results, not calibrated detector measurements. Original results are preserved separately.</p>
<p>Ar/Kr/Xe + CF4; 60%, 80%, 95% noble gas; 1 and 3 atm; constituent C, F and noble recoils;
five logarithmic TRIM energies: 0.1, 0.841, 7.071, 59.460, 500 keV. 20 isolated workers.</p>
<p>Download: <a href="preliminary_trim.csv">new TRIM results (CSV)</a> ·
<a href="previous_trim_1000ions.csv">previous 1,000-ion TRIM results (CSV)</a> ·
<a href="srmodule_quicklook.csv">SRModule ranges and proxy (CSV)</a> · <a href="logs/latest.log">run log</a></p>
{table(coverage, ['family', 'completed', 'planned'])}
<details><summary>Checkpoint details</summary>{table(count_rows, ['stage', 'status', 'jobs'])}</details>
<h2>Full stopping/range scan already available: {len(sr_rows):,} rows</h2>
<p>Source: {html.escape(source)}. Example curves at 80/20 and 1 atm; the CSV contains all available mixtures and pressures.
The electronic stopping proxy does <b>not</b> include the full secondary-recoil ionization calculation and must not be called TRIM QF.
The pressure scan changes gas density in the SRIM model; it does not model pressure-dependent detector response.</p>{sr_image}
<h2>New coarse TRIM scan</h2><p>Points shown only after their checkpoint is complete.
Lines connect simulated energies as a visual guide, not additional simulated points. All mixtures are in the table and CSV.</p>{trim_image}
{table(rows, ['family', 'noble_percent', 'pressure_atm', 'ion', 'energy_kev', 'n_ions', 'projected_range_mm', 'qf_trim_ionization', 'clipped', 'warnings'])}
<h2>Mixture and pressure comparison</h2><p>Noble recoils at the same 7.071 keV energy.
Apparent differences in ionization fraction between pressures can reflect Monte Carlo noise at only 20 ions per point.</p>{mixture_image}
<h2>Previously completed high-statistics TRIM points: {len(previous)}</h2>
<p>1,000 ions per point, from {html.escape(reference_dir.name)}; incomplete coverage, no convergence certification.
TRIM ionization fraction = (primary + secondary-recoil ionization energy) / initial recoil energy.</p>
{table(previous, ['family', 'noble_percent', 'pressure_atm', 'ion', 'energy_kev', 'n_ions', 'projected_range_mm', 'qf_trim_ionization', 'clipped', 'warnings'])}
<p>Report refreshes every minute while the report watcher is running. Check the update time and run state above.</p></html>"""
    atomic_text(run_dir / "report.html", content)
    print(f"{updated}: {state}; {len(rows)}/270 TRIM; report: {run_dir / 'report.html'}", flush=True)
    return running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=ROOT / "runs/noble_cf4_preliminary")
    parser.add_argument("--reference-dir", type=Path, default=ROOT / "runs/noble_cf4_7day_ubuntu")
    parser.add_argument("--watch", action="store_true", help="Refresh every minute until the campaign PID disappears")
    args = parser.parse_args()
    while True:
        running = generate(args.run_dir.resolve(), args.reference_dir.resolve())
        if not args.watch or not running:
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
