"""Fixed-batch precision diagnostics, independent pressure checks and reports."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import warnings

import numpy as np
from scipy import stats
from matplotlib.backends.backend_pdf import PdfPages
import matplotlib.pyplot as plt

from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_report import combine_batches, write_root, energy_figure, scan_figure
from scripts.timing_pilot import completed_rows

FIELDS=("projected_range_mm","qf_trim_ionization")
TARGETS={"projected_range_mm":.05,"qf_trim_ionization":.02}


def key(row):
    return (row["family"],int(row["noble_percent"]),float(row["pressure_atm"]),row["ion"],round(float(row["energy_kev"]),9))


def group_key(row):
    return (row["family"],int(row["noble_percent"]),row["ion"],round(float(row["energy_kev"]),9))


def read_rows(baseline,run):
    rows=[]
    for source in (baseline,run/"refinement"):
        batch,_=completed_rows(source)
        for row in batch:
            row["source_run"]=str(source)
            row["original_energy_kev"]=row["energy_kev"]
            row["energy_kev"]=round(row["energy_kev"],9)
            row["raw_archive"]=str((source/row["raw_archive"]).resolve())
            rows.append(row)
    return rows


def points(rows):
    result=combine_batches(rows,expected_batches=4)
    for row in result:
        n=row["batches_complete"]
        row["all_batches_complete"]=n>=4
        row["precision_met"]=n>=4
        for field in FIELDS:
            sem=row[field+"_batch_sem"]
            relative=sem/max(abs(row[field]),1e-30) if sem is not None else None
            row[field+"_relative_sem"]=relative
            row[field+"_ci95_descriptive_half_width"]=float(stats.t.ppf(.975,n-1))*sem if n>1 else None
            row["precision_met"] &= relative is not None and relative<=TARGETS[field]
        row["precision_flag"]=("needs_four_batches" if n<4 else "statistical_targets_met" if row["precision_met"] else "more_statistics_needed")
    return result


def pressure_tests(rows, confirmation=False):
    """Discovery always uses the first four batches; confirmation uses next four.

    The discovery cohort is fixed before adaptation. Confirmation batches are
    independent of the data used for selecting their pressure group.
    """
    grouped=defaultdict(lambda:defaultdict(list))
    for row in rows:grouped[group_key(row)][float(row["pressure_atm"])].append(row)
    output=[]
    for group,by_pressure in sorted(grouped.items()):
        if len(by_pressure)!=6:continue
        selected=[]
        for pressure,batch in sorted(by_pressure.items()):
            batch=sorted(batch,key=lambda r:r["repeat"])
            subset=batch[4:8] if confirmation else batch[:4]
            if len(subset)!=4:break
            selected.append((pressure,subset))
        if len(selected)!=6:continue
        for field in FIELDS:
            values=[[r[field]*(p if field=="projected_range_mm" else 1) for r in batch] for p,batch in selected]
            means=[statistics.mean(v) for v in values]
            # Zero batch variance makes Welch's statistic undefined. Do not
            # replace it by a fabricated zero p-value or an automatic pass.
            pvalue=None
            if all(statistics.variance(v)>0 for v in values):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore",RuntimeWarning)
                    p=float(stats.f_oneway(*values,equal_var=False).pvalue)
                if math.isfinite(p):pvalue=p
            output.append(dict(family=group[0],noble_percent=group[1],ion=group[2],energy_kev=group[3],
                               quantity=field,relative_pressure_spread=(max(means)-min(means))/max(abs(statistics.mean(means)),1e-30),
                               pvalue=pvalue,adjusted_pvalue=None,flag="indeterminate" if pvalue is None else "not_flagged",
                               cohort="independent_confirmation" if confirmation else "fixed_first_four_batches"))
    finite=[r for r in output if r["pvalue"] is not None]
    if finite:
        adjusted=stats.false_discovery_control([r["pvalue"] for r in finite],method="by")
        for row,value in zip(finite,adjusted):
            row["adjusted_pvalue"]=float(value)
            if value<.05:row["flag"]="pressure_difference_needs_investigation"
    return output


def scientific_selection(rows):
    selected={group_key(r) for r in points(rows) if not r["precision_met"]}
    selected.update(group_key(r) for r in pressure_tests(rows) if r["flag"]!="not_flagged")
    return selected


def publish(run,baseline,state,final=False):
    rows=read_rows(baseline,run)
    combined=points(rows)
    discovery=pressure_tests(rows)
    confirmation=pressure_tests(rows,True)
    output=run/"report";output.mkdir(parents=True,exist_ok=True)
    tables={"points":combined,"batches":rows,"pressure_fixed4":discovery,"pressure_confirmation":confirmation,
            "precision_flagged":[r for r in combined if not r["precision_met"]]}
    for name,data in tables.items():
        fields=list(data[0]) if data else ["no_rows"]
        atomic_text(output/f"{name}.csv",csv_text(data,fields))
    write_root(output/"scan.root",rows,combined)
    summary=dict(expected_points=1890,points_present=len(combined),points_with_four_batches=sum(r["batches_complete"]>=4 for r in combined),
                 points_meeting_statistical_targets=sum(r["precision_met"] for r in combined),
                 fixed4_pressure_tests=len(discovery),independent_pressure_tests=len(confirmation),
                 fixed4_pressure_flags=sum(r["flag"]!="not_flagged" for r in discovery),
                 independent_pressure_flags=sum(r["flag"]!="not_flagged" for r in confirmation),
                 warning="Targets refer to estimated statistical SEM, not model accuracy or guaranteed CI coverage after adaptive selection.")
    atomic_text(run/"science_status.json",json.dumps(summary,indent=2)+"\n")
    if combined:
        with PdfPages(output/"summary.pdf") as pdf:
            for name,fig in (("energy",energy_figure(combined,80,1)),("mixtures",scan_figure(combined,"noble_percent")),("pressure",scan_figure(combined,"pressure_atm"))):
                fig.savefig(output/f"{name}.png",dpi=120);fig.savefig(output/f"{name}.svg")
                pdf.savefig(fig);plt.close(fig)
        if final:
            with PdfPages(output/"atlas.pdf") as pdf:
                for mixture in (60,70,80,90,95):
                    for pressure in (.5,1,1.5,2,2.5,3):
                        fig=energy_figure(combined,mixture,pressure);pdf.savefig(fig);plt.close(fig)
    timestamp=state.get("updated_at","")
    body=f"""<!doctype html><html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>SRIM precision campaign</title><style>body{{font:16px system-ui;max-width:1250px;margin:25px auto;padding:16px}}img{{width:100%}}.scroll{{overflow:auto;max-height:450px}}table{{border-collapse:collapse;font-size:13px}}td,th{{padding:6px;border-bottom:1px solid #ddd;white-space:nowrap}}th{{background:#eef3fa}}</style>
<h1>SRIM timing and statistical precision</h1><p id="fresh"></p>
<p>Ar/Kr/Xe–CF4, 60/70/80/90/95% noble gas, six pressures 0.5–3 atm, C/F/noble recoils,
1/3/10/30/100/300/1000 keV. Baseline data are preserved and identified per batch.</p>
<p><b>Targets:</b> at least four independent 50-ion batches, range SEM ≤5%, ionization-fraction SEM ≤2%.
One adaptive wave may take selected whole pressure groups to eight batches. Maximum 400 ions/point and 72 cumulative hours.</p>
<p>Batch means and Student-t intervals are descriptive statistical estimates. Adaptive selection means the final intervals
do not have guaranteed simultaneous 95% coverage. Physics/model uncertainties and detector calibration are not included.</p>
<p>Pressure tests compare ionization fractions and pressure×range using Welch ANOVA on the fixed first four batches,
with Benjamini–Yekutieli adjustment across all available tests. Independent confirmation uses only the next four batches
at selected groups. A non-significant result does not prove equivalence; a flagged result does not establish new gas physics.</p>
<p><a href="points.csv">Points CSV</a> · <a href="batches.csv">Batch CSV</a> · <a href="scan.root">ROOT</a> ·
<a href="summary.pdf">Summary PDF</a> · <a href="atlas.pdf">Final atlas</a> · <a href="precision_flagged.csv">Unresolved precision</a> ·
<a href="pressure_fixed4.csv">Fixed pressure tests</a> · <a href="pressure_confirmation.csv">Independent pressure tests</a> ·
<a href="../timing_check.json">Timing calibration</a> · <a href="../adaptive_plan.json">Frozen adaptive plan</a> ·
<a href="../logs/campaign.log">Log</a></p>
{table([summary],list(summary))}{table([state],list(state))}
<h2>Energy / mixture / pressure comparisons</h2><img src="energy.png"><img src="mixtures.png"><img src="pressure.png">
<h2>Points requiring follow-up</h2>{table(tables['precision_flagged'],['family','noble_percent','pressure_atm','ion','energy_kev','n_ions_total','projected_range_mm_relative_sem','qf_trim_ionization_relative_sem','precision_flag'])}
<script>const t={json.dumps(timestamp)};document.getElementById('fresh').textContent='Updated '+t+((Date.now()-Date.parse(t)>180000)?' — stale heartbeat; check service status.':'');</script></html>"""
    atomic_text(output/"index.html",body)
    return rows,combined,summary
