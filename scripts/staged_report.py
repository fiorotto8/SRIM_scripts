#!/usr/bin/env python3
"""Checkpoint-backed SRIM-only batch aggregation and publication."""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np
import uproot

from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.timing_pilot import completed_rows

KEYS = ("family", "noble_percent", "pressure_atm", "ion", "energy_kev")


def combine_batches(rows, expected_batches=2):
    groups = {}
    for row in rows:
        groups.setdefault(tuple(row[k] for k in KEYS), []).append(row)
    points = []
    for key, batch in sorted(groups.items()):
        if len({r["repeat"] for r in batch}) != len(batch):
            raise ValueError("Duplicate batch repeat at one physical point")
        if len({r["n_ions"] for r in batch}) != 1:
            raise ValueError("Batch uncertainty requires equal history counts")
        result = dict(zip(KEYS, key))
        result.update(cf4_percent=100-result["noble_percent"], batches_complete=len(batch),
                      batches_expected=expected_batches, n_ions_total=sum(r["n_ions"] for r in batch),
                      job_ids=";".join(r["job_id"] for r in batch),
                      all_batches_complete=len(batch) == expected_batches)
        for field in ("projected_range_mm", "qf_trim_ionization",
                      "primary_ionization_energy_kev", "secondary_recoil_ionization_energy_kev"):
            values = [float(r[field]) for r in batch]
            result[field] = statistics.mean(values)
            result[field + "_batch_sem"] = statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else None
        # Combine the reported longitudinal variances and the between-batch
        # displacement of the projected means, not the standard deviations.
        result["longitudinal_straggling_mm"] = math.sqrt(statistics.mean(
            float(r["longitudinal_straggling_mm"])**2 +
            (float(r["projected_range_mm"])-result["projected_range_mm"])**2 for r in batch))
        result["precision_flag"] = "unreplicated" if len(batch)<2 else "two_batch_estimate_only"
        if len(batch)>1 and any(result[f + "_batch_sem"] > .05 * max(abs(result[f]), 1e-20)
                                for f in ("projected_range_mm", "qf_trim_ionization")):
            result["precision_flag"] = "batch_relative_sem_above_5pct"
        result["clipped"] = any(r.get("clipped",False) for r in batch)
        result["warnings"] = "; ".join(sorted({r.get("warnings","") for r in batch if r.get("warnings")}))
        if result["clipped"] or result["warnings"]:
            result["precision_flag"] = "check_batch_warnings"
        points.append(result)
    return points


def write_root(path, batches, points):
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    with uproot.recreate(temporary) as root:
        for name, rows in (("batches", batches), ("points", points)):
            if not rows:
                continue
            fields = list(rows[0]) if name == "points" else [*KEYS, "repeat", "n_ions", "job_id", "random_seed",
                       "projected_range_mm", "longitudinal_straggling_mm", "qf_trim_ionization",
                       "primary_ionization_energy_kev", "secondary_recoil_ionization_energy_kev", "raw_archive",
                       "wall_seconds_last_attempt", "trim_cpu_seconds_observed", "trim_peak_rss_mib_observed"]
            types, arrays = {}, {}
            for field in fields:
                values = [r.get(field) for r in rows]
                if any(isinstance(v, str) for v in values):
                    types[field] = "string"
                    arrays[field] = [str(v) if v is not None else "" for v in values]
                else:
                    types[field] = "float64"
                    arrays[field] = np.asarray([np.nan if v is None else v for v in values], dtype="float64")
            root.mktree(name, types).extend(arrays)
        root["interpretation"] = "SRIM ionization fraction, not detector calibration. SEM is between equal-size independent batches; two batches give only one variance degree of freedom. Missing SEM is NaN."
    with uproot.open(temporary) as root:
        for name, rows in (("batches", batches), ("points", points)):
            if rows and root[name].num_entries != len(rows):
                raise RuntimeError(f"ROOT row-count verification failed for {name}")
            if rows:
                values = root[name]["qf_trim_ionization"].array(library="np")
                if not np.allclose(values, [r["qf_trim_ionization"] for r in rows], rtol=0, atol=0):
                    raise RuntimeError(f"ROOT value verification failed for {name}")
    temporary.replace(path)


def energy_figure(points, mixture, pressure):
    fig, axes = plt.subplots(2, 3, figsize=(13, 7), layout="constrained")
    for col, family in enumerate(("Ar", "Kr", "Xe")):
        for ion in ("C", "F", family):
            selected = sorted((r for r in points if r["family"]==family and r["ion"]==ion
                               and r["noble_percent"]==mixture and r["pressure_atm"]==pressure), key=lambda r:r["energy_kev"])
            if not selected:
                continue
            for row, field in enumerate(("projected_range_mm", "qf_trim_ionization")):
                axes[row,col].errorbar([r["energy_kev"] for r in selected], [r[field] for r in selected],
                                      yerr=[r[field+"_batch_sem"] or 0 for r in selected], marker="o", label=ion, capsize=3)
        axes[0,col].set_title(f"{family}/CF4 {mixture}/{100-mixture}, {pressure:g} atm")
        for row in range(2):
            axes[row,col].set_xscale("log")
            axes[row,col].grid(alpha=.25)
            if axes[row,col].lines:
                axes[row,col].legend(fontsize=8)
        axes[0,col].set_yscale("log")
        axes[1,col].set_xlabel("Recoil energy [keV]")
    axes[0,0].set_ylabel("Projected range [mm]")
    axes[1,0].set_ylabel("TRIM ionization fraction")
    fig.suptitle("SRIM only · error bars: batch SEM when ≥2 batches; no bar means unreplicated")
    return fig


def scan_figure(points, axis):
    fig, axes = plt.subplots(2, 3, figsize=(13,7), layout="constrained")
    for col, family in enumerate(("Ar","Kr","Xe")):
        for energy in (1,10,100,1000):
            selected = sorted((r for r in points if r["family"]==family and r["ion"]==family
                               and abs(r["energy_kev"]-energy)<.001
                               and (r["pressure_atm"]==1 if axis=="noble_percent" else r["noble_percent"]==80)), key=lambda r:r[axis])
            if not selected:
                continue
            for row, field in enumerate(("projected_range_mm","qf_trim_ionization")):
                axes[row,col].errorbar([r[axis] for r in selected], [r[field] for r in selected],
                                      yerr=[r[field+"_batch_sem"] or 0 for r in selected], marker="o", label=f"{energy:g} keV", capsize=3)
        axes[0,col].set_title(f"{family} recoils · " + ("1 atm" if axis=="noble_percent" else "80/20"))
        for row in range(2):
            axes[row,col].grid(alpha=.25)
            if axes[row,col].lines: axes[row,col].legend(fontsize=8)
        axes[0,col].set_yscale("log")
        axes[1,col].set_xlabel("Noble fraction [%]" if axis=="noble_percent" else "Pressure [atm]")
    axes[0,0].set_ylabel("Projected range [mm]")
    axes[1,0].set_ylabel("TRIM ionization fraction")
    fig.suptitle("SRIM mixture/pressure scan · batch SEM, not model uncertainty")
    return fig


def publish(root, state, final=False):
    production = root / "production"
    rows, counts = completed_rows(production)
    points = combine_batches(rows)
    output = root / "report"
    output.mkdir(parents=True, exist_ok=True)
    if rows:
        atomic_text(output / "batches.csv", csv_text(rows, sorted(set().union(*(r.keys() for r in rows)))))
    if points:
        atomic_text(output / "points.csv", csv_text(points, list(points[0])))
    write_root(output / "scan.root", rows, points)
    complete = sum(r["all_batches_complete"] for r in points)
    plots = []
    if points:
        figures = [("energy", energy_figure(points,80,1)), ("mixtures",scan_figure(points,"noble_percent")),
                   ("pressures",scan_figure(points,"pressure_atm"))]
        temporary = output / f"summary.tmp-{os.getpid()}.pdf"
        with PdfPages(temporary) as pdf:
            for name, fig in figures:
                fig.savefig(output/f"{name}.png", dpi=130)
                fig.savefig(output/f"{name}.svg")
                pdf.savefig(fig)
                plt.close(fig)
                plots.append(f'<h2>{name.title()}</h2><a href="{name}.svg">SVG</a><img src="{name}.png" alt="{name}">')
        temporary.replace(output/"summary.pdf")
    if final and points:
        temporary = output / f"atlas.tmp-{os.getpid()}.pdf"
        with PdfPages(temporary) as pdf:
            for fraction in (60,70,80,90,95):
                for pressure in (.5,1,1.5,2,2.5,3):
                    fig = energy_figure(points, fraction, pressure)
                    pdf.savefig(fig)
                    plt.close(fig)
        temporary.replace(output/"atlas.pdf")
    escaped_state = table([{k:v for k,v in state.items() if k not in {"resources","command"}}],
                          [k for k in state if k not in {"resources","command"}])
    content = f"""<!doctype html><html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>SRIM staged Ar/Kr/Xe scan</title><style>body{{font:16px system-ui;max-width:1250px;margin:30px auto;padding:0 15px}}img{{width:100%}}.scroll{{overflow:auto;max-height:450px}}td,th{{padding:6px;border-bottom:1px solid #ddd;white-space:nowrap}}th{{background:#eef3fa}}table{{border-collapse:collapse;font-size:13px}}</style>
<h1>SRIM Ar/Kr/Xe–CF4 production scan</h1><p id="freshness"></p>
<p><b>{complete}/1,080 physical points at 100 ions; {len(rows)}/2,160 independent 50-ion batches complete.</b></p>
<p>90 gas conditions: Ar/Kr/Xe; 60/70/80/90/95% noble gas; 0.5–3 atm in 0.5-atm steps;
1/10/100/1,000 keV; C, F and noble recoils. No external transport-code comparisons are made.</p>
<p>SRIM ionization fractions are not calibrated detector quenching factors. Two batches give a preliminary SEM
with only one variance degree of freedom. 100 ions does not guarantee convergence. Single-batch points are provisional.
Longitudinal straggling combines batch variances and mean offsets. No pressure-scaled rows are presented as direct simulations.</p>
<p><a href="points.csv">Combined points CSV</a> · <a href="batches.csv">Individual batches CSV</a> ·
<a href="scan.root">ROOT trees</a> · <a href="summary.pdf">Summary PDF</a> · <a href="atlas.pdf">Final atlas PDF</a> ·
<a href="../resource_samples.csv">Resource measurements</a> · <a href="../logs/campaign.log">Log</a> ·
<a href="../gate.json">Commissioning gate</a> · <a href="../final_validation.json">Final validation</a></p>
{escaped_state}<h2>Resources</h2>{table([state.get('resources',{})], list(state.get('resources',{})))}
<h2>Production checkpoint</h2>{table([dict(stage=s,status=t,jobs=n) for s,t,n in counts],['stage','status','jobs'])}
{''.join(plots)}<h2>Combined points</h2>{table(points, [*KEYS,'batches_complete','n_ions_total','projected_range_mm','projected_range_mm_batch_sem','qf_trim_ionization','qf_trim_ionization_batch_sem','precision_flag'])}
<script>const t={json.dumps(state.get('updated_at',''))};const age=(Date.now()-Date.parse(t))/1000;
document.getElementById('freshness').textContent='Last heartbeat: '+t+(age>180?' — stale; run the status command to check the service.':'');</script></html>"""
    atomic_text(output/"index.html",content)
    return rows, points
