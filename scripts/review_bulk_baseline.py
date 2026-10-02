#!/usr/bin/env python3
"""Audit the completed bulk baseline and export readable preliminary results."""
from collections import Counter,defaultdict
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bulk_baseline_retry as prior
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
RUN=prior.RUN
b,scan=prior.b,prior.scan


def review():
    plan,inputs,pilot=prior.prepare(RUN)
    completion=json.loads((RUN/'completion.json').read_text())
    if completion['status']!='REVIEW_REQUIRED' or not completion['source_unchanged']:raise ValueError('Baseline incomplete')
    rows=json.loads((RUN/'results.json').read_text());indexed={r['name']:r for r in rows}
    if len(rows)!=906 or len(indexed)!=906 or set(indexed)!={c['name'] for c in plan['cases']}:
        raise ValueError('Case count or identity mismatch')
    recoveries=0
    for case in plan['cases']:
        row=indexed[case['name']]
        if row!=json.loads((RUN/'cases'/case['name']/'result.json').read_text()):raise ValueError('Result index changed')
        b.previous.audit_case(case,row,inputs[case['name']])
        if 'reused_from' not in row:
            metadata=json.loads((Path(row['attempt'])/'.completion-probes.json').read_text())
            if metadata!=row['completion_monitor'] or metadata['helper']!=str(Path(prior.worker.__file__).resolve()):
                raise ValueError('Completion monitor metadata mismatch')
            recoveries+=sum(x['status']=='recovered' for x in metadata['inspections'])
    combined=b.production_rows(pilot,rows);points=b.p.point_summary(combined)
    if len(combined)!=972 or len({r['seed'] for r in combined})!=972 or len(points)!=243 or not all(p['complete'] for p in points):
        raise ValueError('Independent baseline accounting mismatch')
    if points!=json.loads((RUN/'points.json').read_text()):raise ValueError('Point statistics changed')
    checks=b.geometry_checks(rows)
    if len(checks)!=42 or not all(x['passed'] for x in checks):raise ValueError('Depth gate failed')
    pressure=b.pressure_checks(combined)
    if len(pressure)!=108 or pressure!=json.loads((RUN/'pressure_checks.json').read_text()):raise ValueError('Pressure statistics changed')
    flags=[p for p in points if not p['precision_met']]
    groups=defaultdict(list)
    for row in combined:groups[b.key(row)].append(row['wall_seconds'])
    timing=[dict(family=k[0],ion=k[1],energy_kev=k[2],noble_percent=k[3],pressure_atm=k[4],
                 batches=len(values),mean_seconds=statistics.mean(values),max_seconds=max(values)) for k,values in sorted(groups.items())]
    summary=dict(status='AUDITED_READY_FOR_HIGH_ENERGY_PRESSURE_CANARIES',model_id=b.MODEL,
                 complete_points=243,independent_batches=972,independent_seeds=972,histories=48600,
                 diagnostic_depth_batches=42,diagnostic_histories=2100,points_meeting_precision=243-len(flags),
                 precision_flags=len(flags),range_flags=sum(p['projected_range_mm_relative_sem']>.05 for p in points),
                 ionization_flags=sum(p['ionization_fraction_relative_sem']>.02 for p in points),
                 flag_counts_by_energy={f'{e:g}':count for e,count in sorted(Counter(p['energy_kev'] for p in flags).items())},
                 pressure_classifications=dict(Counter(p['classification'] for p in pressure)),
                 max_depth_ionization_relative_change=max(abs(c['qf_relative_change']) for c in checks),
                 max_depth_range_relative_change=max(abs(c['dx_relative_change']) for c in checks),
                 recovery_wall_hours=completion['cumulative_hours']-plan['inherited_used_seconds']/3600,
                 cumulative_baseline_hours=completion['cumulative_hours'],resources=completion['resources'],
                 recovered_monitor_races=recoveries,longest_successful_batch_seconds=max(r['wall_seconds'] for r in combined),
                 decision='Proceed to the fixed 18-point 1MeV pressure timing/containment gate at 80% noble and 0.5/3atm. '
                 'Preserve all 46 estimated-precision flags for fixed independent follow-ups. The 108 prespecified pressure '
                 'intervals include 106 inconclusive and two inside working margins; zero intervals are entirely outside '
                 'the margins. Pressure equivalence remains unresolved. Four-batch SEM measures sampling precision; '
                 'SRIM ionization fraction still requires detector calibration to become detector quenching.')
    return summary,combined,points,timing,pressure


def plots(points,out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(out/'baseline.pdf') as pdf:
        for family in ('Ar','Kr','Xe'):
            fig,axes=plt.subplots(3,2,figsize=(11,12),squeeze=False)
            for i,ion in enumerate(('C','F',family)):
                for noble in (60,80,95):
                    group=sorted((p for p in points if p['family']==family and p['ion']==ion and p['noble_percent']==noble and p['pressure_atm']==1),key=lambda p:p['energy_kev'])
                    for j,metric in enumerate(('projected_range_mm','ionization_fraction')):
                        ax=axes[i,j];ax.errorbar([p['energy_kev'] for p in group],[p[metric] for p in group],
                                               yerr=[p[metric+'_sem'] for p in group],marker='o',capsize=2,label=f'{noble}% noble')
                        ax.set_xscale('log');ax.set_xlabel('Energy [keV]');ax.grid(True,alpha=.3);ax.legend()
                        ax.set_title(f'{family}/CF4, {ion} recoil, 1 atm')
                        ax.set_ylabel('Projected displacement [mm]' if j==0 else 'SRIM ionization fraction')
                        if j==0:ax.set_yscale('log')
            fig.suptitle('Independent bulk baseline: 200 ions/point; error bars = batch SEM')
            fig.tight_layout();pdf.savefig(fig);fig.savefig(out/(family+'_baseline.png'),dpi=110);plt.close(fig)
            fig,axes=plt.subplots(3,2,figsize=(11,12),squeeze=False)
            for i,ion in enumerate(('C','F',family)):
                for pressure in (.5,1.,3.):
                    group=sorted((p for p in points if p['family']==family and p['ion']==ion and p['noble_percent']==80 and p['pressure_atm']==pressure and p['energy_kev'] in (1,10,100)),key=lambda p:p['energy_kev'])
                    for j,metric in enumerate(('projected_range_mm','ionization_fraction')):
                        ax=axes[i,j];scale=pressure if j==0 else 1
                        ax.errorbar([p['energy_kev'] for p in group],[p[metric]*scale for p in group],
                                    yerr=[p[metric+'_sem']*scale for p in group],marker='o',capsize=2,label=f'{pressure:g} atm')
                        ax.set_xscale('log');ax.set_xlabel('Energy [keV]');ax.grid(True,alpha=.3);ax.legend()
                        ax.set_title(f'{family}80/CF4-20, {ion} recoil')
                        ax.set_ylabel('Pressure × displacement [atm mm]' if j==0 else 'SRIM ionization fraction')
                        if j==0:ax.set_yscale('log')
            fig.suptitle('Pressure slice: descriptive batch SEM; pressure equivalence unresolved')
            fig.tight_layout();pdf.savefig(fig);plt.close(fig)


def main():
    summary,rows,points,timing,pressure=review();out=RUN/'review'
    write_json(out/'summary.json',summary)
    for name,data in [('points',points),('timing',timing),('precision_flags',[p for p in points if not p['precision_met']]),('pressure_checks',pressure)]:
        atomic_text(out/(name+'.csv'),csv_text(data,sorted({k for r in data for k in r})))
    plots(points,out)
    body='<html><meta charset="utf-8"><title>Audited bulk baseline</title><h1>Ar/Kr/Xe bulk baseline: preliminary results</h1>'
    body+='<p>'+summary['decision']+'</p><p><a href="baseline.pdf">Six-page PDF</a> · '
    body+='<a href="points.csv">243 points and SEMs</a> · <a href="precision_flags.csv">46 precision flags</a> · '
    body+='<a href="pressure_checks.csv">Prespecified pressure screen</a> · <a href="timing.csv">Measured timings</a></p>'
    body+=table([summary],list(summary))
    for family in ('Ar','Kr','Xe'):body+=f'<h2>{family}</h2><img src="{family}_baseline.png" style="max-width:100%">'
    atomic_text(out/'index.html',body);print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
