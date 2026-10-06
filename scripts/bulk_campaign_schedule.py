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
    mixture_anchor = (family, ion, energy, noble, 1.)
    if energy != 1000 and mixture_anchor in means:
        def pressure_ratio(q):
            same_energy = (family, ion, energy, 80, q)
            if same_energy in means:
                return means[same_energy]/means[family, ion, energy, 80, 1.]
            return max(means[family, ion, e, 80, q]/means[family, ion, e, 80, 1.] for e in (1., 10., 100.))
        ratio = 1. if pressure == 1. else pressure_ratio(.5) if pressure == .5 else legacy.interpolate(pressure, 1., 3., 1., pressure_ratio(3.))
        return means[mixture_anchor]*ratio
    if energy != 1000 or not all(k in means for k in anchors):
        return legacy.batch_cost(key, means)
    base = means[mixture_anchor] if mixture_anchor in means else legacy.batch_cost(mixture_anchor, means)
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


def g5_estimate(keys, batches, means, workers=15):
    """Measured P4 pressure factors and mixture means; include sequential gates."""
    phases = {'low': [], 'high': [], 'rest': []}
    if not keys or batches != 4 or workers < 1:
        raise ValueError('Expected a nonempty fixed four-batch wave')
    for f, ion, e, noble, pressure in keys:
        if noble == 80 or pressure == 1.:
            raise ValueError('G5 contains only missing non1atm mixture points')
        cost = means[f, ion, e, noble, 1.]*max(
            1., means[f, ion, e, 80, pressure]/means[f, ion, e, 80, 1.])
        for repeat in range(batches):
            phase = 'low' if repeat == 0 and e == 1. else 'high' if repeat == 0 and e == 1000. else 'rest'
            phases[phase].append(cost)
    models = {}
    for phase, costs in phases.items():
        loads = [0.] * workers
        for cost in sorted(costs, reverse=True):
            heapq.heapreplace(loads, loads[0]+cost)
        models[phase] = max(loads)/3600
    model = sum(models.values())
    return dict(points=len(keys), batches=batches*len(keys), histories=50*batches*len(keys),
                model_hours=round(model, 2), planning_hours=round(1.5*model+.5, 2),
                longest_batch_hours=round(max(c for costs in phases.values() for c in costs)/3600, 2),
                phase_model_hours=models, forecast_basis='P4_same_energy_pressure_factors_and_measured_1atm_mixtures')


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
            estimator = g5_estimate if stage['id'] == 'G5' and all(
                (k[0], k[1], k[2], 80, k[4]) in means for k in keys) else estimate
            rows_out.append(dict(id=stage['id']+(f'-{wave}' if wave is not None else ''),
                                 **estimator(keys,stage['batches_per_point'],means),
                                 cap_hours=stage.get('cap_hours',stage.get('cap_hours_per_wave'))))
    for r in rows_out:r['fits_cap']=r['planning_hours']<=r['cap_hours']
    totals=dict(direct_points=3024,surface_points_reused=0,initial_batches=sum(r['batches'] for r in rows_out),
                initial_histories=sum(r['histories'] for r in rows_out),
                baseline_planning_hours=round(sum(r['planning_hours'] for r in rows_out),2))
    if len(initial)!=27 or len(rebuild)!=216 or totals['initial_batches']!=12096:raise ValueError('Bulk baseline accounting changed')
    return rows_out,totals


def main():
    from scripts import historical_execution as history
    from scripts import bulk_baseline_rebuild as rebuild
    from scripts import review_bulk_baseline as bulk_review
    roadmap=json.loads(legacy.PLAN.read_text())
    with history.historical_sources():
        review,raw,*_=pilot.previous.review()
        pilot_review,_,_,_=rebuild.previous.review()
        baseline_review,_,_,timing,_=bulk_review.review()
    measured={(r['family'],r['ion'],r['energy_kev'],r['noble_percent'],r['pressure_atm']):r['mean_seconds'] for r in timing}
    high_review=None; high_rows=[]; high_sources=[]
    from scripts import review_high_energy_pressure as high_audit
    if (high_audit.RUN/'completion.json').exists():
        with history.historical_sources():
            high_review,high_rows,*_=high_audit.review()
        measured.update({rebuild.key(r):r['wall_seconds'] for r in high_rows})
        high_sources=[Path(high_audit.__file__),high_audit.RUN/'completion.json',high_audit.RUN/'results.json']
    batch_review=None; pressure_sources=[]
    from scripts import review_pressure_batches as pressure_audit
    if (pressure_audit.RUN/'completion.json').exists():
        batch_review,_,_,timing,_=pressure_audit.review()
        measured={(r['family'],r['ion'],r['energy_kev'],r['noble_percent'],r['pressure_atm']):r['mean_seconds'] for r in timing}
        pressure_sources=[Path(pressure_audit.__file__),pressure_audit.RUN/'completion.json',pressure_audit.RUN/'results.json']
    mixture_review=None; mixture_sources=[]
    from scripts import review_mixtures as mixture_audit
    if (mixture_audit.RUN/'completion.json').exists():
        mixture_review,_,_,timing,_=mixture_audit.review()
        measured={rebuild.key(r):r['mean_seconds'] for r in timing}
        mixture_sources=[Path(mixture_audit.__file__),mixture_audit.RUN/'completion.json',
                         mixture_audit.RUN/'results.json',mixture_audit.RUN/'points.json']
    p4_review=None; p4_sources=[]
    from scripts import review_pressure_grid as p4_audit
    if (p4_audit.RUN/'completion.json').exists():
        p4_review,_,_,timing,_=p4_audit.review()
        measured={rebuild.key(r):r['mean_seconds'] for r in timing}
        p4_sources=[Path(p4_audit.__file__),p4_audit.RUN/'completion.json',p4_audit.RUN/'results.json',
                    p4_audit.RUN/'points.json',p4_audit.RUN/'pressure_checks.json']
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
    if batch_review is not None:
        next(row for row in rows if row['id']=='P2b').update(status='completed_and_reviewed',actual_hours=batch_review['actual_hours'])
    mixture_plan=ROOT/'runs/noble_cf4_mixtures_1atm/plan.json'
    if mixture_plan.exists():
        frozen=json.loads(mixture_plan.read_text())
        next(row for row in rows if row['id']=='M3').update(model_hours=round(frozen['forecast']['model_hours'],2),
                                                         planning_hours=round(frozen['forecast']['planning_hours'],2),
                                                         longest_batch_hours=round(frozen['forecast']['longest_estimated_batch_hours'],2),
                                                         status='frozen_direct_mixture_scan_review_gated')
    mixture_retry_plan=ROOT/'runs/noble_cf4_mixtures_1atm_retry/plan.json'
    mixture_retry=json.loads(mixture_retry_plan.read_text()) if mixture_retry_plan.exists() else None
    if mixture_retry is not None:
        next(row for row in rows if row['id']=='M3').update(
            status='M3R_frozen_recovery_review_gated',
            model_hours=round(mixture_retry['forecast']['model_hours'],2),
            planning_hours=round(mixture_retry['forecast']['planning_hours'],2),
            retained_original_cases=len(mixture_retry['retained_original_cases']),
            remaining_original_cases=mixture_retry['new_cases_to_run'],
            inherited_cumulative_hours=round(mixture_retry['inherited_used_seconds']/3600,4))
    reset_plan=ROOT/'runs/xvfb_reset_gate/plan.json'
    reset_review=None
    if reset_plan.exists():
        from scripts import xvfb_reset_gate as reset_gate
        reset=json.loads(reset_plan.read_text())
        reset_gate.verify_hashes(reset['input_sha256'])
        reset_review=reset['review']
        remaining=reset_review['remaining_frozen_forecast']
        next(row for row in rows if row['id']=='M3').update(
            status='401_batches_audited_859_remaining_held_for_C1e_review',
            model_hours=round(remaining['model_hours'],2),planning_hours=round(remaining['planning_hours'],2),
            longest_batch_hours=round(remaining['longest_estimated_batch_hours'],2),
            retained_original_cases=401,remaining_original_cases=859,
            inherited_cumulative_hours=round(reset_review['inherited_used_seconds']/3600,4),
            pending_diagnostic_cap_hours=.25,
            fits_cumulative_cap_with_diagnostic_and_drain=
                remaining['planning_hours']+reset_review['inherited_used_seconds']/3600+.25+3.025<=24)
    noreset_plan=ROOT/'runs/noble_cf4_mixtures_1atm_noreset/plan.json'
    noreset=None
    if noreset_plan.exists():
        noreset=json.loads(noreset_plan.read_text())
        reset_gate.verify_hashes(noreset['input_sha256'])
        next(row for row in rows if row['id']=='M3').update(
            status='M3R2_validated_noreset_continuation_review_gated',
            inherited_cumulative_hours=round(noreset['inherited_used_seconds']/3600,4),
            pending_diagnostic_cap_hours=0,
            fits_cumulative_cap_with_diagnostic_and_drain=
                noreset['forecast']['planning_including_spent_hours']+3.025<=24)
    p4_plan=ROOT/'runs/noble_cf4_pressure_grid/plan.json'
    if p4_plan.exists():
        fixed=json.loads(p4_plan.read_text())
        reset_gate.verify_hashes(fixed['input_sha256'])
        next(row for row in rows if row['id']=='P4').update(
            status='frozen_direct_pressure_grid_review_gated',model_hours=round(fixed['forecast']['model_hours'],2),
            planning_hours=round(fixed['forecast']['planning_hours'],2),
            longest_batch_hours=round(fixed['forecast']['longest_estimated_batch_hours'],2))
    if mixture_review is not None:
        next(row for row in rows if row['id']=='M3').update(status='completed_and_reviewed',
            actual_hours=mixture_review['cumulative_m3_hours'],remaining_original_cases=0,retained_original_cases=1260)
    if p4_review is not None:
        next(row for row in rows if row['id']=='P4').update(status='completed_and_reviewed',
            actual_hours=p4_review['actual_hours'],completed_independent_points=243)
    g5_plan=ROOT/'runs/noble_cf4_mixture_pressure_60/plan.json'
    if g5_plan.exists():
        frozen=json.loads(g5_plan.read_text())
        reset_gate.verify_hashes(frozen['input_sha256'])
        next(row for row in rows if row['id']=='G5-60').update(status='frozen_review_gated_wave',
            model_hours=round(frozen['forecast']['model_hours'],2),
            planning_hours=round(frozen['forecast']['planning_hours'],2))
    rows[1].update(status='completed_and_reviewed',actual_hours=baseline_review['cumulative_baseline_hours'],
                   remaining_original_cases=0,completed_independent_points=243)
    for row in rows:row['fits_cap']=row['planning_hours']<=row['cap_hours']
    future=rows[4:] if batch_review is not None else rows[3:] if high_review is not None else rows[2:]
    if mixture_review is not None:future=[row for row in future if row['id']!='M3']
    if p4_review is not None:future=[row for row in future if row['id']!='P4']
    accepted_points=261 if batch_review is not None else 243
    accepted_batches=1044 if batch_review is not None else 972+len(high_rows)
    retained_mixture=len(mixture_retry['retained_original_cases']) if mixture_retry is not None else 0
    accepted_batches+=retained_mixture
    partial_points=retained_mixture if batch_review is not None else len(high_rows)
    mixture_spent=mixture_retry['inherited_used_seconds']/3600 if mixture_retry is not None else 0
    if reset_review is not None:
        accepted_points=reset_review['combined_complete_points']
        accepted_batches=reset_review['combined_independent_batches']
        partial_points=reset_review['partial_points']
        mixture_spent=reset_review['inherited_used_seconds']/3600
    if noreset is not None:
        mixture_spent=noreset['inherited_used_seconds']/3600
    if mixture_review is not None:
        accepted_points=mixture_review['complete_points']; accepted_batches=mixture_review['independent_batches']
        partial_points=0; mixture_spent=mixture_review['cumulative_m3_hours']
    if p4_review is not None:
        accepted_points=p4_review['complete_points']; accepted_batches=p4_review['independent_batches']; partial_points=0
    totals.update(accepted_points=accepted_points,partial_points=partial_points,
                  accepted_independent_batches=accepted_batches,accepted_histories=50*accepted_batches,
                  remaining_baseline_points=3024-accepted_points,remaining_baseline_batches=12096-accepted_batches,
                  remaining_baseline_histories=50*(12096-accepted_batches),
                  remaining_planning_hours=round(sum(row['planning_hours'] for row in future),2),
                  elapsed_plus_remaining_hours=round(pilot_review['actual_hours']+baseline_review['cumulative_baseline_hours']+
                                                     (high_review['actual_hours'] if high_review is not None else 0)+
                                                     (batch_review['actual_hours'] if batch_review is not None else 0)+
                                                     mixture_spent+
                                                     (p4_review['actual_hours'] if p4_review is not None else 0)+
                                                     sum(row['planning_hours'] for row in future),2),
                  baseline_planning_hours=round(sum(row['planning_hours'] for row in rows),2))
    current=next(s for s in roadmap['stages'] if s['id']==roadmap['current_stage_id'])
    environment_diagnostics=roadmap.get('bulk_model_transition',{}).get('additional_c1d_diagnostic_histories',0)
    environment_diagnostics+=roadmap.get('bulk_model_transition',{}).get('additional_c1e_diagnostic_histories',0)
    totals['post_upgrade_diagnostic_histories_additional']=environment_diagnostics
    totals['diagnostic_histories_additional']+=environment_diagnostics
    status=ROOT/current['run']/'status.json'
    sources=[Path(__file__),Path(rebuild.__file__),Path(bulk_review.__file__),legacy.PLAN,
             bulk_review.RUN/'completion.json',bulk_review.RUN/'results.json',pilot.RUN/'results.json']
    if rebuild_plan.exists():sources.append(rebuild_plan)
    if recovery_plan.exists():sources.append(recovery_plan)
    if high_plan.exists():sources.append(high_plan)
    if batch_plan.exists():sources.append(batch_plan)
    if mixture_plan.exists():sources.append(mixture_plan)
    if mixture_retry_plan.exists():sources.append(mixture_retry_plan)
    if reset_plan.exists():sources.append(reset_plan)
    if noreset_plan.exists():sources.append(noreset_plan)
    if p4_plan.exists():sources.append(p4_plan)
    if g5_plan.exists():sources.append(g5_plan)
    sources.extend(p4_sources)
    sources.extend(mixture_sources)
    sources.extend(high_sources)
    sources.extend(pressure_sources);sources.extend([Path(history.__file__),history.EVIDENCE,history.HISTORICAL,history.LIVE])
    summary=dict(generated_at=datetime.now().astimezone().isoformat(),model_id=pilot.MODEL,
                 status='MEASURED_BULK_BASELINE_FORECAST_LATER_STAGES_REVIEW_GATED',totals=totals,stages=rows,
                 current_stage=current['id'],current_status=json.loads(status.read_text()) if status.exists() else None,
                 assumptions='After P4 completion review,819directpoints/3276independentbatches are accepted,141precision flags remain. '
                 'G5 uses measured same-energy80%-noble pressure/1atm cost ratios floored atone, '
                 'times measured1atm means for each mixture. Separate45low/45high canary phases gate1170remaining batches. '
                 'Seven315-point waves remain; onlyG5-60 is released and every subsequent wave requires review. '
                 'The630 pressure screens are per-wave,not campaign-wide error control or independent confirmation. '
                 'Earlier snapshot descriptions below apply only before the corresponding completion audit. '
                 'After M3 completion review,576directpoints/2304independentbatches were accepted,99precision flags remained. '
                 'All504one-atm mixture timings then use measured four-batch means,not concentration interpolation. '
                 'P4 frozen timing uses maximum observed pressure-anchor costs and separate45low/27high canary phases. '
                 'Earlier snapshot descriptions below apply only before the corresponding completion audit. '
                 'Later stages require their own review. Costs use measured means from all 972 independent bulk batches, '
                 'plus the eighteen pressure points using four-batch P2b means when complete (single P2a times beforehand). '
                 'Historical executable audit uses a recovered hash-identical Xvfb after the documented post-P2b OS upgrade; '
                 'new production additionally requires the current environment gate. At 1 MeV, use measured same-energy '
                 '80%-noble pressure ratios; other mixtures inherit those cost factors and intermediate pressures '
                 'interpolate the 1/3-atm timing anchors. This is a timing estimate, not a pressure/mixture physics result. '
                 'C3b diagnostic repeats are excluded from timing means and statistics; actual cumulative completion time is retained. '
                 'Mixture and pressure cost interpolation/extrapolation are planning only, not simulated physics points. '
                 'Once M3 is frozen, its adjacent-anchor maximum four-batch costs and sequential low-energy gate '
                 'replace the earlier linearly interpolated M3 runtime estimate. '
                 'When C1e is prepared, counts use its full raw-audited M3R stop snapshot:401 M3 batches, '
                 '350 combined complete points and45partial points;859 M3 batches remain. The remaining M3 '
                 'forecast retains frozen adjacent-anchor costs,not a linear fraction of elapsed time. '
                 'C1e diagnostic replays do not count as production;when M3R2 is frozen its reviewed6.47minutes '
                 'are included in inherited cumulative time. Both original and noreset controls passed,so reset causality is unproved. '
                 'Before C1e, accepted counts use the ten-batch M3R recovery-start snapshot. No live unaudited pooling. '
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
