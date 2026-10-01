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


def forecast(roadmap,rows,calibrated_means=None):
    old=legacy.timing_means(legacy.TIMINGS)
    interior={(r['family'],r['ion'],r['energy_kev']):r for r in rows if r['geometry']=='interior' and 'point' in r}
    means={}
    for k,value in old.items():
        f,ion,e,n,p=k
        factor=max(1.,interior[f,ion,1000.]['wall_seconds']/old[f,ion,1000.,80,1.])
        overhead=max(0.,interior[f,ion,1.]['wall_seconds']-old[f,ion,1.,80,1.])
        means[k]=value*factor+overhead
    if calibrated_means is not None:means=calibrated_means
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
    from scripts import bulk_baseline_rebuild as rebuild
    pilot_review,_,_,timing=rebuild.previous.review()
    rows,totals=forecast(roadmap,raw,rebuild.calibrated_timings(timing))
    rows[0].update(actual_hours=pilot_review['actual_hours'],status='completed_and_reviewed')
    rebuild_plan=rebuild.RUN/'plan.json'
    if rebuild_plan.exists():
        frozen=json.loads(rebuild_plan.read_text())
        rows[1].update(model_hours=round(frozen['forecast']['model_hours'],2),
                       planning_hours=round(frozen['forecast']['planning_hours'],2),
                       diagnostic_batches=frozen['diagnostic_batches'],diagnostic_histories=frozen['diagnostic_histories'])
    for row in rows:row['fits_cap']=row['planning_hours']<=row['cap_hours']
    totals.update(remaining_planning_hours=round(sum(r['planning_hours'] for r in rows[1:]),2),
                  elapsed_plus_remaining_hours=round(pilot_review['actual_hours']+sum(r['planning_hours'] for r in rows[1:]),2),
                  baseline_planning_hours=round(sum(r['planning_hours'] for r in rows),2),
                  diagnostic_histories_additional=2100)
    current=next(s for s in roadmap['stages'] if s['id']==roadmap['current_stage_id'])
    status=ROOT/current['run']/'status.json'
    sources=[Path(__file__),Path(rebuild.__file__),legacy.PLAN,legacy.TIMINGS,pilot.RUN/'results.json']
    if rebuild_plan.exists():sources.append(rebuild_plan)
    summary=dict(generated_at=datetime.now().astimezone().isoformat(),model_id=pilot.MODEL,
                 status='PROVISIONAL_BULK_MODEL_FORECAST_NOT_PRODUCTION_RELEASE',totals=totals,stages=rows,
                 current_stage=current['id'],current_status=json.loads(status.read_text()) if status.exists() else None,
                 assumptions='Full-grid transition remains gated by baseline review. Pilot raw audit passed, with five precision flags retained. '
                 'No surface or paired-diagnostic histories reused in statistics. Costs use measured surface timings, increased by '
                 'the maximum measured pilot 100/1000keV bulk-to-surface ratio (never below one), plus nonnegative 1keV startup overhead. '
                 'C3b includes 42 separately counted depth diagnostics and sequential release gates. '
                 'Mixture and pressure cost interpolation/extrapolation are planning only, not simulated physics points. '
                 '15 workers, 50% margin plus 30 minutes per stage/wave. Replace estimates with measured pilot/rebuild times. '
                 'Does not include precision top-ups, user callbacks, active review or new defects; Q6 allowance remains conditional.',
                 input_sha256={str(p):pilot.scan.sha256_file(p) for p in sources})
    atomic_text(OUT/'forecast.json',json.dumps(summary,indent=2)+'\n')
    columns=list(dict.fromkeys(k for row in rows for k in row))
    atomic_text(OUT/'baseline_schedule.csv',csv_text(rows,columns))
    body='<html><meta charset="utf-8"><title>Gated bulk campaign schedule</title><h1>Provisional bulk-model schedule</h1>'
    body+='<p>'+summary['assumptions']+'</p><p><strong>No old surface points are counted as bulk results.</strong></p>'
    body+=table([totals],list(totals))+table(rows,columns)
    body+='<p><a href="../../configs/campaign_roadmap.json">Full gates and contingencies</a> · '
    body+=f'<a href="../{Path(current["run"]).name}/index.html">Current live report</a></p>'
    atomic_text(OUT/'index.html',body);print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
