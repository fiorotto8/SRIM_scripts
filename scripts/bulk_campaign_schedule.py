#!/usr/bin/env python3
"""Conditional bulk-model roadmap: no reuse of the 243 surface points; never launch."""
from __future__ import annotations
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import campaign_schedule as legacy
from scripts import bulk_precision_pilot as pilot
from scripts.preliminary_report import atomic_text,csv_text,table
OUT=ROOT/'runs/bulk_campaign_schedule'


def pilot_keys(target):
    return {k for k in legacy.existing_grid(target) if k[2] in (1,100,1000) and k[3:]==(80,1.)}


def forecast(roadmap,rows):
    old=legacy.timing_means(legacy.TIMINGS)
    interior={(r['family'],r['ion'],r['energy_kev']):r for r in rows if r['geometry']=='interior' and 'point' in r}
    means={}
    for k,value in old.items():
        f,ion,e,n,p=k
        factor=max(1.,interior[f,ion,1000.]['wall_seconds']/old[f,ion,1000.,80,1.])
        overhead=max(0.,interior[f,ion,1.]['wall_seconds']-old[f,ion,1.,80,1.])
        means[k]=value*factor+overhead
    target=roadmap['target'];initial=pilot_keys(target);rebuild=legacy.existing_grid(target)-initial
    groups=legacy.selections(target);rows_out=[]
    for stage,keys,cap in [('C3a',initial,6),('C3b',rebuild,12)]:
        rows_out.append(dict(id=stage,**legacy.estimate(keys,4,means),cap_hours=cap))
    for stage in roadmap['stages']:
        if 'selection' not in stage:continue
        for wave in stage.get('wave_noble_percent',[None]):
            keys={k for k in groups[stage['selection']] if wave is None or k[3]==wave}
            rows_out.append(dict(id=stage['id']+(f'-{wave}' if wave is not None else ''),
                                 **legacy.estimate(keys,stage['batches_per_point'],means),
                                 cap_hours=stage.get('cap_hours',stage.get('cap_hours_per_wave'))))
    for r in rows_out:r['fits_cap']=r['planning_hours']<=r['cap_hours']
    totals=dict(direct_points=3024,surface_points_reused=0,initial_batches=sum(r['batches'] for r in rows_out),
                initial_histories=sum(r['histories'] for r in rows_out),
                baseline_planning_hours=round(sum(r['planning_hours'] for r in rows_out),2))
    if len(initial)!=27 or len(rebuild)!=216 or totals['initial_batches']!=12096:raise ValueError('Bulk baseline accounting changed')
    return rows_out,totals


def main():
    roadmap=json.loads(legacy.PLAN.read_text());review,raw,*_=pilot.previous.review()
    rows,totals=forecast(roadmap,raw)
    current=next(s for s in roadmap['stages'] if s['id']==roadmap['current_stage_id'])
    status=ROOT/current['run']/'status.json'
    summary=dict(generated_at=datetime.now().astimezone().isoformat(),model_id=pilot.MODEL,
                 status='PROVISIONAL_BULK_MODEL_FORECAST_NOT_PRODUCTION_RELEASE',totals=totals,stages=rows,
                 current_stage=current['id'],current_status=json.loads(status.read_text()) if status.exists() else None,
                 assumptions='Full-grid transition remains gated by the independent pilot and baseline review. '
                 'No surface or paired-diagnostic histories reused. Costs use measured surface timings, increased by '
                 'the nonnegative interior 1MeV cost ratio and added low-energy startup overhead for each family/recoil. '
                 'Mixture and pressure cost interpolation/extrapolation are planning only, not simulated physics points. '
                 '15 workers, 50% margin plus 30 minutes per stage/wave. Replace estimates with measured pilot/rebuild times. '
                 'Does not include precision top-ups, user callbacks, active review or new defects; Q6 allowance remains conditional.',
                 input_sha256={str(p):pilot.scan.sha256_file(p) for p in
                               [Path(__file__),legacy.PLAN,legacy.TIMINGS,pilot.previous.RUN/'results.json']})
    atomic_text(OUT/'forecast.json',json.dumps(summary,indent=2)+'\n')
    atomic_text(OUT/'baseline_schedule.csv',csv_text(rows,list(rows[0])))
    body='<html><meta charset="utf-8"><title>Gated bulk campaign schedule</title><h1>Provisional bulk-model schedule</h1>'
    body+='<p>'+summary['assumptions']+'</p><p><strong>No old surface points are counted as bulk results.</strong></p>'
    body+=table([totals],list(totals))+table(rows,list(rows[0]))
    body+='<p><a href="../../configs/campaign_roadmap.json">Full gates and contingencies</a> · '
    body+=f'<a href="../{Path(current["run"]).name}/index.html">Current live report</a></p>'
    atomic_text(OUT/'index.html',body);print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
