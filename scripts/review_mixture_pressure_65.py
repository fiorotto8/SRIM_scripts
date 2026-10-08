#!/usr/bin/env python3
"""Cold review of the completed65% wave, including all preserved continuations."""
from collections import Counter,defaultdict
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import mixture_pressure_65_ten as g
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json

RUN=ROOT/'runs/noble_cf4_mixture_pressure_65_ten'
r,p,b,scan=g.r,g.r.p,g.r.b,g.r.scan


def read(name):
    return json.loads((RUN/name).read_text())


def review():
    original,inputs,baseline,retained,_,_=r.review.review()
    plan=read('plan.json'); r.old.gate.verify_hashes(plan['input_sha256'])
    state=read('status.json'); thermal=read('thermal-status.json'); exited=read('service-exit.json')
    if (state!=read('completion.json') or state['status']!='REVIEW_REQUIRED'
            or not state['source_unchanged'] or state['completed_continuation_batches']!=1009
            or state['completed_batches']!=1260 or state['worker_process_limit']!=10
            or state['software_temperature_cutoff_c'] is not None
            or [exited[k] for k in ('service_result','exit_code','exit_status')]!=['success','exited','0']
            or thermal['status']!='COMPLETE' or thermal['child_returncode']!=0
            or state['resources']['memory_swap_current_gib']!=0 or state['resources']['memory_peak_gib']>32):
        raise ValueError('G5-65 completion/resource/service gate failed')
    rows=read('results.json'); indexed={row['name']:row for row in rows}
    if (len(rows)!=1260 or set(indexed)!={x['name'] for x in original['cases']}
            or plan['cases']!=original['cases'] or plan['retained_original_names']!=sorted(x['name'] for x in retained)):
        raise ValueError('G5-65 exact cohort changed')
    paths={RUN/n for n in ('plan.json','status.json','completion.json','results.json','points.json','pressure_checks.json',
                          'thermal-status.json','thermal-samples.jsonl','throttle-warnings.jsonl','budget.json',
                          'service-exit.json','session.json','logs/campaign.log','pressure.pdf')}
    paths.add(Path(__file__))
    saved=r.RUN; r.RUN=RUN
    try:
        with r.old.gate.retry.parser_context():
            for case in original['cases']:
                row=indexed[case['name']]
                owner=r.old.RUN if case['name'] in plan['retained_original_names'] else RUN
                path=owner/'cases'/case['name']/'result.json'
                if row!=json.loads(path.read_text()) or row.get('diagnostic'):
                    raise ValueError('Accepted row or ownership changed')
                (r.old.audit_case if owner==r.old.RUN else r.audit_case)(case,row,inputs[case['name']])
                paths.add(path); paths.update(Path(x) for x in row['artifact_sha256'])
    finally:
        r.RUN=saved
    combined=baseline+rows; points=b.p.point_summary(combined)
    if (len(combined)!=5796 or len({x['seed'] for x in combined})!=5796 or len(points)!=1449
            or not all(x['complete'] for x in points) or points!=read('points.json')):
        raise ValueError('Combined independent statistics changed')
    checks=r.old.pressure_checks(combined,plan['pressure_screen'])
    if checks!=read('pressure_checks.json') or any(x['classification']=='followup' for x in checks):
        raise ValueError('Pressure follow-up or changed statistics; hold next wave')
    base=thermal['baseline']; last=base; peak=max(base['temperatures_c'].values()); count=0; warnings=0; maxgap=0
    with (RUN/'thermal-samples.jsonl').open() as stream:
        for line in stream:
            sample=json.loads(line); g.check(sample,base,last)
            gap=(datetime.fromisoformat(sample['time'])-datetime.fromisoformat(last['time'])).total_seconds()
            if not 0<gap<5: raise ValueError('Thermal trace discontinuity')
            maxgap=max(maxgap,gap); warnings+=sample['throttle_counts']!=last['throttle_counts']
            peak=max(peak,max(sample['temperatures_c'].values())); last=sample; count+=1
    start=datetime.fromisoformat(thermal['started_at']); end=datetime.fromisoformat(thermal['ended_at'])
    if (count!=thermal['samples'] or last!=thermal['latest'] or peak!=thermal['peak_temperature_c']
            or warnings!=thermal['throttle_warning_samples']
            or not 0<=(datetime.fromisoformat(base['time'])-start).total_seconds()<5
            or not 0<=(end-datetime.fromisoformat(last['time'])).total_seconds()<5):
        raise ValueError('Thermal trace accounting changed')
    elapsed=(datetime.fromisoformat(exited['ended_at'])-start).total_seconds()
    budget=read('budget.json')
    if (abs(budget['used_seconds']-plan['inherited_used_seconds']-elapsed)>.1
            or budget['used_seconds']>86400 or budget['limit_seconds']!=86400):
        raise ValueError('Cumulative budget/cleanup mismatch')
    groups=defaultdict(list)
    for row in combined: groups[b.key(row)].append(row['wall_seconds'])
    timing=[dict(family=k[0],ion=k[1],energy_kev=k[2],noble_percent=k[3],pressure_atm=k[4],
                 batches=len(v),mean_seconds=statistics.mean(v),max_seconds=max(v)) for k,v in sorted(groups.items())]
    flags=[x for x in points if not x['precision_met']]
    summary=dict(status='AUDITED_READY_FOR_G5_70',complete_points=1449,independent_batches=5796,histories=289800,
                 points_meeting_precision=1449-len(flags),precision_flags=len(flags),
                 flags_by_energy=dict(Counter(str(x['energy_kev']) for x in flags)),
                 flags_by_family=dict(Counter(x['family'] for x in flags)),
                 pressure_classes=dict(Counter(x['classification'] for x in checks)),
                 final_continuation_hours=elapsed/3600,cumulative_wave_hours=budget['used_seconds']/3600,
                 peak_temperature_c=peak,thermal_samples=count,throttle_warning_samples=warnings,max_telemetry_gap_seconds=maxgap,
                 resources=state['resources'],longest_batch_seconds=max(x['wall_seconds'] for x in rows),
                 decision='All1260original batches audited,including251retained and1009ten-worker continuations. '
                          '213precision flags remain forfixedQ6;noadaptive topups. Pressure intervals are screening,not equivalence. '
                          '102Csampledpeak andhardware throttling acknowledged underuserapproved no-softwarecutoff policy. '
                          'Next70%wave uses10workers,production only,no loadtest;splitifbudgetrequires. '
                          'Ionization fraction isnot detector-calibrated quenching;nativecrash rootcause remainsunproven.')
    manifest={**plan['input_sha256'],**{str(x):p.history.actual_sha256(x) for x in sorted(paths)}}
    return summary,combined,points,timing,checks,manifest


def main():
    summary,_,points,timing,checks,_=review(); out=RUN/'review'
    write_json(out/'summary.json',summary)
    for name,data in [('points',points),('timing',timing),('pressure_checks',checks),('precision_flags',[x for x in points if not x['precision_met']])]:
        atomic_text(out/(name+'.csv'),csv_text(data,sorted({k for x in data for k in x})))
    atomic_text(out/'index.html','<html><meta charset="utf-8"><h1>Audited65% noble/CF4 wave</h1><p>'+summary['decision']+
                '</p><p><a href="../pressure.pdf">Pressure plots</a> · <a href="points.csv">Points andSEM</a> · '
                '<a href="precision_flags.csv">Precision flags</a></p>'+table([summary],list(summary)))
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
