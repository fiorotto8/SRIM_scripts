#!/usr/bin/env python3
"""Audit complete C2d corners and distinguish geometry checks from precision."""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bulk_corner_retry as prior
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
RUN=prior.RUN


def review():
    plan,inputs=prior.prepare(RUN)
    completion=json.loads((RUN/'completion.json').read_text())
    if completion['status']!='REVIEW_REQUIRED' or not completion['source_unchanged']:raise ValueError('Corner validation incomplete')
    rows=json.loads((RUN/'results.json').read_text())
    if len(rows)!=37 or len({r['name'] for r in rows})!=37:raise ValueError('Corner count/identity mismatch')
    for case in plan['cases']:
        r=json.loads((RUN/'cases'/case['name']/'result.json').read_text())
        if r!=next(x for x in rows if x['name']==case['name']) or r['status']!='COMPLETE':raise ValueError('Result index mismatch')
        if any(r[k]!=v for k,v in case.items()):raise ValueError('Case identity changed')
        for path,digest in r['artifact_sha256'].items():
            if prior.scan.sha256_file(Path(path))!=digest:raise ValueError('Raw artifacts changed')
        directory=Path(r['attempt'])
        if (directory/'TRIM.IN').read_bytes()!=inputs[case['name']][0] or (directory/'TRIM.DAT').read_bytes()!=inputs[case['name']][1]:
            raise ValueError('Executed input mismatch')
        fresh=prior.base.analyse(case,{p.name.upper():p for p in directory.iterdir()})
        if any(r[k]!=v for k,v in fresh.items()):raise ValueError('Raw analysis mismatch')
        ack=json.loads((directory/'completion-ack.json').read_text())
        if ack['status']!='sent' or ack['title']!=prior.base.completion.TITLE:raise ValueError('Completion evidence missing')
        if json.loads((directory/'.trim-progress.json').read_text())['status']!='complete':raise ValueError('Normal exit missing')
        if 'reused_from' not in r:
            meta=json.loads((directory/'.display-startup.json').read_text())
            if meta['allocation']!='Xvfb -displayfd' or not meta['authenticated'] or meta['tcp_listening']:
                raise ValueError('Private display evidence missing')
    checks=prior.base.depth_checks(rows)
    if len(checks)!=18 or not all(r['passed'] for r in checks):raise ValueError('Paired-depth gate failed')
    points=[r for r in rows if r['geometry']=='interior' and 'point' in r]
    summary=dict(status='AUDITED_READY_FOR_INDEPENDENT_BULK_PILOT',completed_cases=37,retained_cases=19,new_high_energy_cases=18,
                 max_depth_qf_relative_change=max(abs(r['qf_relative_change']) for r in checks),
                 max_depth_range_relative_change=max(abs(r['dx_relative_change']) for r in checks),
                 retry_wall_hours=completion['cumulative_hours']-plan['inherited_used_seconds']/3600,
                 cumulative_hours=completion['cumulative_hours'],resources=completion['resources'],
                 longest_batch_seconds=max(r['wall_seconds'] for r in rows),independent_production_histories_added=0,
                 interpretation='The 18 species/energy depth pairs and exact replay pass. No primary escapes or profile-edge signal '
                 'in these selected cases. This establishes an implementation/geometry gate, not independent-batch precision, '
                 'pressure equivalence, full-grid model accuracy or detector-calibrated quenching. '
                 'Use central starts in 2W targets provisionally for a fresh independent-batch pilot. '
                 'Projected range is mean endpoint X minus initial X over all 50 contained primaries. '
                 'Surface-injection data and paired diagnostic histories are excluded from the new bulk baseline.',
                 input_sha256={str(p):prior.scan.sha256_file(p) for p in
                               [Path(__file__),RUN/'plan.json',RUN/'completion.json',RUN/'results.json']})
    return summary,rows,checks,points


def main():
    summary,rows,checks,points=review();out=RUN/'review'
    write_json(out/'summary.json',summary)
    cols=['family','ion','energy_kev','ionization_fraction','stopped_ion_mean_dx_mm','wall_seconds']
    atomic_text(out/'diagnostic_points.csv',csv_text(points,cols))
    atomic_text(out/'depth_checks.csv',csv_text(checks,list(checks[0])))
    body='<html><meta charset="utf-8"><title>C2d audited review</title><h1>Interior corner validation review</h1>'
    body+='<p>'+summary['interpretation']+'</p>'+table([summary],list(summary))
    body+='<h2>Single-batch diagnostic points — no precision certification</h2>'+table(points,cols)
    body+='<h2>Paired-depth checks</h2>'+table(checks,list(checks[0]))
    atomic_text(out/'index.html',body);print(json.dumps(summary,indent=2));print('Report:',out/'index.html')


if __name__=='__main__':main()
