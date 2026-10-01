#!/usr/bin/env python3
"""Raw-data audit of the fixed independent bulk pilot (never launch simulations)."""
import json
from pathlib import Path
import statistics
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bulk_precision_pilot as pilot
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
RUN=pilot.RUN


def audit_case(case, row, contents):
    if row['status']!='COMPLETE' or any(row[k]!=v for k,v in case.items()):
        raise ValueError('Incomplete or changed case')
    directory=Path(row['attempt'])
    required={'TRIM.IN','TRIM.DAT','TDATA.TXT','IONIZ.TXT','RANGE.TXT','RANGE_3D.TXT',
              'BACKSCAT.TXT','TRANSMIT.TXT','completion-ack.json','.trim-progress.json','.display-startup.json'}
    if not required<={Path(p).name for p in row['artifact_sha256']}:raise ValueError('Missing raw hashes')
    for path,digest in row['artifact_sha256'].items():
        if pilot.scan.sha256_file(Path(path))!=digest:raise ValueError('Raw artifact changed')
    if (directory/'TRIM.IN').read_bytes()!=contents[0] or (directory/'TRIM.DAT').read_bytes()!=contents[1]:
        raise ValueError('Executed input changed')
    fresh=pilot.base.analyse(case,{p.name.upper():p for p in directory.iterdir()})
    if any(row[k]!=v for k,v in fresh.items()):raise ValueError('Raw analysis mismatch')
    ack=json.loads((directory/'completion-ack.json').read_text())
    progress=json.loads((directory/'.trim-progress.json').read_text())
    meta=json.loads((directory/'.display-startup.json').read_text())
    if ack['status']!='sent' or ack['title']!=pilot.base.completion.TITLE or progress['status']!='complete':
        raise ValueError('Normal completion evidence missing')
    if meta['status']!='READY' or meta['allocation']!='Xvfb -displayfd' or not meta['authenticated'] or meta['tcp_listening']:
        raise ValueError('Private display evidence missing')
    if meta!=row['display_startup']:raise ValueError('Display metadata mismatch')


def review():
    plan,inputs=pilot.prepare(RUN)
    completion=json.loads((RUN/'completion.json').read_text())
    if completion['status']!='REVIEW_REQUIRED' or not completion['source_unchanged']:raise ValueError('Pilot incomplete')
    rows=json.loads((RUN/'results.json').read_text());indexed={r['name']:r for r in rows}
    if len(rows)!=108 or len(indexed)!=108 or set(indexed)!={r['name'] for r in plan['cases']}:
        raise ValueError('Pilot count/identity mismatch')
    for case in plan['cases']:
        row=json.loads((RUN/'cases'/case['name']/'result.json').read_text())
        if row!=indexed[case['name']]:raise ValueError('Result index mismatch')
        audit_case(case,row,inputs[case['name']])
    points=pilot.point_summary(rows)
    if len(points)!=27 or not all(p['complete'] for p in points):raise ValueError('Incomplete independent points')
    if points!=json.loads((RUN/'points.json').read_text()):raise ValueError('Point summary mismatch')
    timing=[]
    for f,ion,e in sorted({(r['family'],r['ion'],r['energy_kev']) for r in rows}):
        rr=[r['wall_seconds'] for r in rows if (r['family'],r['ion'],r['energy_kev'])==(f,ion,e)]
        timing.append(dict(family=f,ion=ion,energy_kev=e,batches=len(rr),mean_seconds=statistics.mean(rr),max_seconds=max(rr)))
    flags=[p for p in points if not p['precision_met']]
    summary=dict(status='AUDITED_READY_FOR_GATED_BASELINE_REBUILD',model_id=pilot.MODEL,
                 cases=108,independent_seeds=len({r['seed'] for r in rows}),points=27,histories=5400,
                 points_meeting_precision=27-len(flags),precision_flags=flags,
                 actual_hours=completion['cumulative_hours'],resources=completion['resources'],
                 longest_batch_seconds=max(r['wall_seconds'] for r in rows),
                 max_range_relative_sem=max(p['projected_range_mm_relative_sem'] for p in points),
                 max_ionization_relative_sem=max(p['ionization_fraction_relative_sem'] for p in points),
                 decision='Retain all 27 compatible bulk points and their precision flags. Proceed to the fixed 216-point '
                 'baseline rebuild with mixture/pressure geometry gates. No automatic top-ups or surface-data reuse. '
                 'Five precision shortfalls are statistical flags, not integrity failures; reserve fixed independent top-ups for Q6. '
                 'SEM from four batch means is estimated precision, not model accuracy or detector quenching.')
    return summary,rows,points,timing


def main():
    summary,rows,points,timing=review();out=RUN/'review'
    write_json(out/'summary.json',summary)
    for name,data in [('points',points),('timing',timing),('precision_flags',summary['precision_flags'])]:
        atomic_text(out/(name+'.csv'),csv_text(data,list(data[0]) if data else []))
    body='<html><meta charset="utf-8"><title>Independent bulk pilot review</title><h1>Audited bulk pilot</h1>'
    body+='<p>'+summary['decision']+'</p>'+table([summary],list(summary))+table(points,list(points[0]))
    body+='<h2>Measured 50-ion batch times</h2>'+table(timing,list(timing[0]))
    atomic_text(out/'index.html',body);print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
