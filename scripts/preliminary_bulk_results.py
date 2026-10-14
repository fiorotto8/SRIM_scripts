#!/usr/bin/env python3
"""Readable, descriptive snapshot of the audited bulk data; no simulations."""
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json

SOURCE=ROOT/'runs/noble_cf4_mixture_pressure_70a'
OUT=ROOT/'runs/noble_cf4_preliminary_20261009'


def select(points,family,ion,energy,noble,pressure=1.):
    rows=[x for x in points if (x['family'],x['ion'],x['energy_kev'],x['noble_percent'],x['pressure_atm'])==
          (family,ion,energy,noble,pressure)]
    if len(rows)!=1 or not rows[0]['complete']:raise ValueError('Missing or duplicate complete point')
    return rows[0]


def examples(points):
    rows=[]
    for family in ('Ar','Kr','Xe'):
        x=select(points,family,family,10,80)
        rows.append(dict(gas=family+'80/CF4-20',recoil=family,energy_kev=10,pressure_atm=1,
                         forward_displacement_um=1000*x['projected_range_mm'],
                         displacement_sem_um=1000*x['projected_range_mm_sem'],
                         ionization_percent=100*x['ionization_fraction'],
                         ionization_sem_percentage_points=100*x['ionization_fraction_sem'],
                         precision_met=x['precision_met']))
    return rows


def render(points,summary,out,source=SOURCE):
    if len(points)!=summary.get('complete_points') or not points or not all(x['complete'] and x['batches']==4 and x['ions']==200 for x in points):
        raise ValueError('Expected audited four-batch snapshot')
    if (len({(x['family'],x['ion'],x['energy_kev'],x['noble_percent'],x['pressure_atm']) for x in points})!=len(points)
            or sum(not x['precision_met'] for x in points)!=summary['precision_flags']):
        raise ValueError('Audit summary mismatch')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    out.mkdir(parents=True,exist_ok=True)
    ex=examples(points);ends=[]
    for family in ('Ar','Kr','Xe'):
        a,z=(select(points,family,family,10,n) for n in (60,95))
        ends.append(dict(recoil_and_gas=family,energy_kev=10,pressure_atm=1,
                         ionization_percent_at60=100*a['ionization_fraction'],
                         ionization_percent_at95=100*z['ionization_fraction'],
                         displacement_um_at60=1000*a['projected_range_mm'],
                         displacement_um_at95=1000*z['projected_range_mm']))
    with PdfPages(out/'preliminary_results.pdf') as pdf:
        fig,axes=plt.subplots(1,2,figsize=(12,5))
        for family in ('Ar','Kr','Xe'):
            rr=[select(points,family,family,e,80) for e in (1,3,10,30,100,300,1000)]
            for ax,metric,factor in zip(axes,('projected_range_mm','ionization_fraction'),(1000,100)):
                ax.errorbar([r['energy_kev'] for r in rr],[factor*r[metric] for r in rr],
                            yerr=[factor*r[metric+'_sem'] for r in rr],marker='o',capsize=3,label=family+' recoil in '+family+'/CF4')
                ax.set_xscale('log');ax.set_xlabel('Recoil energy [keV]');ax.grid(alpha=.3);ax.legend(fontsize=8)
        axes[0].set_yscale('log');axes[0].set_ylabel('Mean forward displacement [µm]')
        axes[1].set_ylabel('SRIM ionization fraction [%]')
        fig.suptitle('80/20 noble/CF4 at 1 atm: own-noble recoils (different projectile species)')
        fig.text(.5,.015,'Error bars: SEM across 4 independent 50-ion batches. Statistical errors only; not calibrated detector quenching.',ha='center',fontsize=9)
        fig.tight_layout(rect=(0,.055,1,.95));pdf.savefig(fig);fig.savefig(out/'energy_overview.png',dpi=150);plt.close(fig)
        for metric,factor,ylabel in [('projected_range_mm',1000,'Mean forward displacement [µm]'),
                                     ('ionization_fraction',100,'SRIM ionization fraction [%]')]:
            fig,axes=plt.subplots(1,3,figsize=(14,4.8))
            for ax,category in zip(axes,('C','F','noble')):
                for family in ('Ar','Kr','Xe'):
                    ion=family if category=='noble' else category
                    rr=[select(points,family,ion,10,n) for n in (60,65,70,75,80,85,90,95)]
                    ax.errorbar([r['noble_percent'] for r in rr],[factor*r[metric] for r in rr],
                                yerr=[factor*r[metric+'_sem'] for r in rr],marker='o',capsize=3,label=family+'/CF4')
                ax.set_title(category+' recoil');ax.set_xlabel('Noble mole fraction [%]');ax.set_ylabel(ylabel);ax.grid(alpha=.3);ax.legend()
            fig.suptitle('10 keV, 1 atm: complete mixture scan; means ± batch SEM')
            fig.text(.5,.015,'Descriptive comparisons, not simultaneous confidence intervals. Mixture changes both composition and density.',ha='center',fontsize=9)
            fig.tight_layout(rect=(0,.055,1,.95));pdf.savefig(fig);plt.close(fig)
        fig,axes=plt.subplots(1,2,figsize=(12,5))
        for family in ('Ar','Kr','Xe'):
            rr=[select(points,family,family,10,80,pressure) for pressure in (.5,1,1.5,2,2.5,3)]
            for ax,metric in zip(axes,('projected_range_mm','ionization_fraction')):
                factors=[1000*r['pressure_atm'] if metric=='projected_range_mm' else 100 for r in rr]
                ax.errorbar([r['pressure_atm'] for r in rr],[f*r[metric] for f,r in zip(factors,rr)],
                            yerr=[f*r[metric+'_sem'] for f,r in zip(factors,rr)],marker='o',capsize=3,label=family+' recoil')
                ax.set_xlabel('Pressure [atm]');ax.grid(alpha=.3);ax.legend()
        axes[0].set_ylabel('Pressure × mean forward displacement [atm µm]');axes[1].set_ylabel('SRIM ionization fraction [%]')
        fig.suptitle('10 keV, 80/20 noble/CF4: pressure dependence with batch SEM')
        fig.text(.5,.015,'Flat pressure×displacement would be consistent withinverse-pressure scaling; these plots do not establish equivalence.',ha='center',fontsize=9)
        fig.tight_layout(rect=(0,.055,1,.95));pdf.savefig(fig);plt.close(fig)
    for name,rows in [('points',points),('examples_10kev',ex),('mixture_endpoints_10kev',ends),
                      ('precision_flags',[x for x in points if not x['precision_met']])]:
        atomic_text(out/(name+'.csv'),csv_text(rows,sorted({k for x in rows for k in x})))
    display=[dict(gas=x['gas'],recoil=x['recoil'],displacement_um=f"{x['forward_displacement_um']:.2f} ± {x['displacement_sem_um']:.2f}",
                  ionization_percent=f"{x['ionization_percent']:.2f} ± {x['ionization_sem_percentage_points']:.2f}") for x in ex]
    text='<html><meta charset="utf-8"><title>Preliminary SRIM results</title><style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:0 20px;line-height:1.6}td,th{padding:8px;border:1px solid #ddd}table{border-collapse:collapse}</style><h1>Useful preliminary results — audited snapshot</h1>'
    text+=f"<p>Audited snapshot: {len(points):,}/3,024 points ({100*len(points)/3024:.1f}%), {summary['independent_batches']:,} independent batches, {summary['histories']:,} ion histories. {summary['points_meeting_precision']:,} points meet estimated 5% range / 2% ionization relative SEM targets; {summary['precision_flags']} remain flagged.</p>"
    text+='<p><a href="preliminary_results.pdf">Four-page comparison PDF</a> · <a href="points.csv">All points and SEM (CSV)</a> · <a href="precision_flags.csv">Precision flags</a></p>'
    text+='<h2>10 keV own-noble recoils at 1 atm, 80/20 noble/CF4</h2><p>Mean ± SEM across four independent 50-ion batches. Different gases here also use different recoil species; this is not a controlled identical-projectile comparison.</p>'+table(display,list(display[0]))
    text+='<h2>Mixture trend at 10 keV, 1 atm</h2><p>From 60% to 95% noble, simulated ionization fraction decreases: Ar 28.7% → 25.7%, Kr 22.6% → 16.1%, Xe 19.9% → 12.1%. These are descriptive endpoint comparisons, not a claim that every adjacent step is significant. The PDF also compares identical C/F projectiles.</p>'
    coverage=[]
    for noble in sorted({x['noble_percent'] for x in points}):
        pressures=sorted({x['pressure_atm'] for x in points if x['noble_percent']==noble})
        coverage.append(f"{noble:g}% noble: {', '.join(f'{q:g}' for q in pressures)} atm")
    text+='<h2>Scope and limitations</h2><p>Range means forward endpoint-minus-start displacement, not total track length. SRIM ionization fraction is not detector-calibrated quenching. SEM covers batch sampling uncertainty, not SRIM/model/systematic uncertainty. No interpolated or surface-injection results are pooled. Most pressure confidence intervals remain inconclusive; no equivalence claim. All precision flags stay visible for fixed Q6 follow-up.</p><p>Completed coverage: '+'; '.join(coverage)+'.</p>'
    text+=f'<img src="energy_overview.png" style="max-width:100%"><p><a href="../{source.name}/review/index.html">Full source-stage audit</a></p>'
    atomic_text(out/'index.html',text)
    write_json(out/'provenance.json',dict(generated_at=datetime.now().astimezone().isoformat(),audit_summary=summary,
        input_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in
                      (source/'points.json',source/'review/summary.json',Path(__file__))}))
    return ex


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,default=SOURCE);parser.add_argument('--out',type=Path,default=OUT)
    args=parser.parse_args()
    points=json.loads((args.source/'points.json').read_text());summary=json.loads((args.source/'review/summary.json').read_text())
    if summary['status'] not in ('AUDITED_READY_FOR_G5_70B','AUDITED_READY_FOR_G5_75','AUDITED_READY_FOR_G5_75B','AUDITED_READY_FOR_G5_85','AUDITED_READY_FOR_G5_85B','AUDITED_READY_FOR_G5_90'):raise ValueError('Cold audit required first')
    print(json.dumps(render(points,summary,args.out,args.source),indent=2))
