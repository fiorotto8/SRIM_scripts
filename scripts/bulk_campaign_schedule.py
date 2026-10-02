#!/usr/bin/env python3
"""Conditional bulk-model roadmap: no reuse of the 243 surface points; never launch."""
from __future__ import annotations
from datetime import datetime
import heapq
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


def batch_cost(key, means):
    if key in means:
        return means[key]
    family, ion, energy, noble, pressure = key
    anchors = [(family, ion, 1000., 80, p) for p in (.5, 1., 3.)]
    if energy != 1000 or not all(k in means for k in anchors):
        return legacy.batch_cost(key, means)
    base = legacy.batch_cost((family, ion, energy, noble, 1.), means)
    ratio = 1. if pressure == 1. else means[anchors[0]] / means[anchors[1]] if pressure == .5 else \
        legacy.interpolate(pressure, 1., 3., 1., means[anchors[2]] / means[anchors[1]])
    return base * ratio


def estimate(keys, batches, means, workers=15):
    costs = sorted((batch_cost(k, means) for k in keys for _ in range(batches)), reverse=True)
    if not costs or batches < 1 or workers < 1:
        raise ValueError('Expected nonempty work and positive batch/worker counts')
    loads = [0.] * workers
    for cost in costs:
        heapq.heapreplace(loads, loads[0] + cost)
    model = max(loads) / 3600
    return dict(points=len(keys), batches=len(costs), histories=50*len(costs),
                model_hours=round(model, 2), planning_hours=round(1.5*model+.5, 2),
                longest_batch_hours=round(costs[0]/3600, 2))


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
        rows_out.append(dict(id=stage,**estimate(keys,4,means),cap_hours=cap))
    for stage in roadmap['stages']:
        if 'selection' not in stage:continue
        for wave in stage.get('wave_noble_percent',[None]):
            keys={k for k in groups[stage['selection']] if wave is None or k[3]==wave}
            rows_out.append(dict(id=stage['id']+(f'-{wave}' if wave is not None else ''),
                                 **estimate(keys,stage['batches_per_point'],means),
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
    from scripts import review_bulk_baseline as bulk_review
    pilot_review,_,_,_=rebuild.previous.review()
    baseline_review,_,_,timing,_=bulk_review.review()
    measured={(r['family'],r['ion'],r['energy_kev'],r['noble_percent'],r['pressure_atm']):r['mean_seconds'] for r in timing}
    high_review=None; high_rows=[]; high_sources=[]
    from scripts import review_high_energy_pressure as high_audit
    if (high_audit.RUN/'completion.json').exists():
        high_review,high_rows,*_=high_audit.review()
        measured.update({rebuild.key(r):r['wall_seconds'] for r in high_rows})
        high_sources=[Path(high_audit.__file__),high_audit.RUN/'completion.json',high_audit.RUN/'results.json']
    rows,totals=forecast(roadmap,raw,measured)
    rows[0].update(actual_hours=pilot_review['actual_hours'],status='completed_and_reviewed')
    rebuild_plan=rebuild.RUN/'plan.json'
    if rebuild_plan.exists():
        frozen=json.loads(rebuild_plan.read_text())
        rows[1].update(model_hours=round(frozen['forecast']['model_hours'],2),
                       planning_hours=round(frozen['forecast']['planning_hours'],2),
                       diagnostic_batches=frozen['diagnostic_batches'],diagnostic_histories=frozen['diagnostic_histories'])
    recovery_plan=ROOT/'runs/noble_cf4_bulk_baseline_retry/plan.json'
    recovery=json.loads(recovery_plan.read_text()) if recovery_plan.exists() else None
    for row in rows:row['fits_cap']=row['planning_hours']<=row['cap_hours']
    totals.update(remaining_planning_hours=round(sum(r['planning_hours'] for r in rows[1:]),2),
                  elapsed_plus_remaining_hours=round(pilot_review['actual_hours']+sum(r['planning_hours'] for r in rows[1:]),2),
                  baseline_planning_hours=round(sum(r['planning_hours'] for r in rows),2),
                  diagnostic_histories_additional=2100)
    if recovery is not None:
        rows[1].update(retained_original_cases=len(recovery['retained_cases']),
                       remaining_original_cases=recovery['new_cases_to_run'],
                       recovery_planning_hours=round(recovery['forecast']['planning_hours'],2),
                       inherited_cumulative_hours=round(recovery['inherited_used_seconds']/3600,4))
        totals.update(remaining_planning_hours=round(recovery['forecast']['planning_hours']+sum(r['planning_hours'] for r in rows[2:]),2),
                      elapsed_plus_remaining_hours=round(pilot_review['actual_hours']+recovery['inherited_used_seconds']/3600+
                                                         recovery['forecast']['planning_hours']+sum(r['planning_hours'] for r in rows[2:]),2),
                      diagnostic_histories_additional=2101)
    high_plan=ROOT/'runs/noble_cf4_high_energy_pressure/plan.json'
    if high_plan.exists():
        high=json.loads(high_plan.read_text())
        stage=next(row for row in rows if row['id']=='P2a')
        stage.update(model_hours=round(high['forecast']['model_hours'],2),planning_hours=round(high['forecast']['planning_hours'],2),
                     longest_batch_hours=round(high['forecast']['longest_estimated_batch_hours'],2))
    if high_review is not None:
        next(row for row in rows if row['id']=='P2a').update(status='completed_and_reviewed',actual_hours=high_review['actual_hours'])
    batch_plan=ROOT/'runs/noble_cf4_high_energy_pressure_batches/plan.json'
    if batch_plan.exists():
        frozen=json.loads(batch_plan.read_text())
        next(row for row in rows if row['id']=='P2b').update(model_hours=round(frozen['forecast']['model_hours'],2),
                                                          planning_hours=round(frozen['forecast']['planning_hours'],2),
                                                          longest_batch_hours=round(frozen['forecast']['longest_estimated_batch_hours'],2))
    rows[1].update(status='completed_and_reviewed',actual_hours=baseline_review['cumulative_baseline_hours'],
                   remaining_original_cases=0,completed_independent_points=243)
    for row in rows:row['fits_cap']=row['planning_hours']<=row['cap_hours']
    future=rows[3:] if high_review is not None else rows[2:]
    totals.update(accepted_points=243,partial_points=len(high_rows),accepted_independent_batches=972+len(high_rows),
                  accepted_histories=48600+50*len(high_rows),
                  remaining_baseline_points=2781,remaining_baseline_batches=11124-len(high_rows),remaining_baseline_histories=556200-50*len(high_rows),
                  remaining_planning_hours=round(sum(row['planning_hours'] for row in future),2),
                  elapsed_plus_remaining_hours=round(pilot_review['actual_hours']+baseline_review['cumulative_baseline_hours']+
                                                     (high_review['actual_hours'] if high_review is not None else 0)+
                                                     sum(row['planning_hours'] for row in future),2),
                  baseline_planning_hours=round(sum(row['planning_hours'] for row in rows),2))
    current=next(s for s in roadmap['stages'] if s['id']==roadmap['current_stage_id'])
    status=ROOT/current['run']/'status.json'
    sources=[Path(__file__),Path(rebuild.__file__),Path(bulk_review.__file__),legacy.PLAN,
             bulk_review.RUN/'completion.json',bulk_review.RUN/'results.json',pilot.RUN/'results.json']
    if rebuild_plan.exists():sources.append(rebuild_plan)
    if recovery_plan.exists():sources.append(recovery_plan)
    if high_plan.exists():sources.append(high_plan)
    if batch_plan.exists():sources.append(batch_plan)
    sources.extend(high_sources)
    summary=dict(generated_at=datetime.now().astimezone().isoformat(),model_id=pilot.MODEL,
                 status='MEASURED_BULK_BASELINE_FORECAST_LATER_STAGES_REVIEW_GATED',totals=totals,stages=rows,
                 current_stage=current['id'],current_status=json.loads(status.read_text()) if status.exists() else None,
                 assumptions='The complete 243-point bulk baseline passed raw audit; 46 precision flags remain. '
                 'Later stages require their own review. Costs use measured means from all 972 independent bulk batches, '
                 'plus the eighteen P2a single-batch timings when complete. At 1 MeV, use measured same-energy '
                 '80%-noble pressure ratios; other mixtures inherit those cost factors and intermediate pressures '
                 'interpolate the 1/3-atm timing anchors. This is a timing estimate, not a pressure/mixture physics result. '
                 'C3b diagnostic repeats are excluded from timing means and statistics; actual cumulative completion time is retained. '
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
