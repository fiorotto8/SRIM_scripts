#!/usr/bin/env python3
"""Fixed independent precision and pressure confirmation; no grid expansion."""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime
import fcntl
import heapq
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time
import warnings

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import srim_scan as scan
from scipy import stats
from scripts import corrected_refinement as previous
from scripts import precision_analysis as analysis
from scripts.precision_scan import job_key
from scripts.staged_scan import configured_jobs,write_json
from scripts.timing_pilot import completed_rows,resource_sample,memory_info,GIB
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_report import write_root,energy_figure,scan_figure

RUN=ROOT/'runs/noble_cf4_corrected_closeout'
CONFIG=ROOT/'configs/noble_cf4_corrected_closeout.toml'
UNIT='srim-corrected-closeout.service'
MODEL=previous.base.MODEL
BUDGET=3*3600


def choose(rows,jobs):
    points=analysis.points(rows)
    flagged={analysis.key(p) for p in points if not p['precision_met']}
    checks=previous.pressure_checks(rows)
    groups={(p['family'],p['ion'],p['energy_kev']) for p in checks
            if p['relative_mean_spread']>(.10 if p['metric']=='projected_range_mm' else .05)}
    keys=flagged|{(f,80,p,ion,e) for f,ion,e in groups for p in (.5,1.,3.)}
    if len(points)!=243 or len(flagged)!=35 or len(groups)!=6 or len(keys)!=47:
        raise ValueError('Expected reviewed 35 precision points plus 12 controls in six pressure groups')
    if any(p['n_ions_total']!=200 for p in points if analysis.key(p) in flagged):
        raise ValueError('Precision-selected points must start with 200 ions')
    selected=[j for j in jobs if job_key(j) in keys]
    if len(selected)!=564:raise ValueError('Expected exactly 564 new batches')
    for key in keys:
        group=[j for j in selected if job_key(j)==key]
        if len(group)!=12 or {j.payload['repeat'] for j in group}!=set(range(100,112)) or any(j.payload['n_ions']!=50 for j in group):
            raise ValueError('Expected twelve fresh 50-ion batches per point')
    seeds={r['random_seed'] for r in rows}
    for j in selected:
        seed=scan.seed_for(j.job_id,j.payload['repeat'])
        if seed in seeds:raise ValueError('Duplicate history seed')
        seeds.add(seed)
    return selected,flagged,groups,keys


def prepare(run):
    completion=json.loads((previous.RUN/'completion.json').read_text())
    if completion['summary']['points_complete']!=243 or completion['summary']['unresolved_precision_points']!=35:
        raise ValueError('Expected reviewed completed 243-point refinement')
    old=json.loads((previous.RUN/'plan.json').read_text())
    inherited={**old['input_sha256'],**old['inherited_sha256']}
    for path,digest in inherited.items():
        if scan.sha256_file(ROOT/path)!=digest:raise ValueError('Frozen source inputs changed')
    cfg=scan.load_config(CONFIG)
    if cfg.raw['physics']!=scan.load_config(previous.CONFIGS['topup']).raw['physics'] or cfg.temperature_k!=293.15:
        raise ValueError('Physical model changed')
    rows=previous.read_rows(previous.RUN)
    sr,jobs=configured_jobs(CONFIG)
    jobs,flags,groups,keys=choose(rows,jobs)
    pairs={(j.payload['condition']['condition_id'],j.payload['ion']) for j in jobs}
    sr=[j for j in sr if (j.payload['condition']['condition_id'],j.payload['ion']) in pairs]
    times=defaultdict(list)
    for row in rows:times[analysis.key(row)].append(row['wall_seconds_last_attempt'])
    workers=[0.]*15
    for value in sorted((statistics.mean(times[job_key(j)]) for j in jobs),reverse=True):heapq.heapreplace(workers,workers[0]+value)
    paths=[CONFIG,Path(__file__),ROOT/'scripts/corrected_closeout.sh',previous.RUN/'plan.json',previous.RUN/'completion.json',
           previous.RUN/'report/points.csv',previous.RUN/'report/pressure_checks.csv',previous.RUN/'report/precision_flagged.csv']
    for row in rows:paths.extend((Path(row['raw_archive']),Path(row['normalized_profiles']),Path(row['source_run'])/'job_results/trim'/f"{row['job_id']}.json"))
    plan=dict(model_id=MODEL,input_sha256={str(p.relative_to(ROOT)):scan.sha256_file(p) for p in paths},inherited_sha256=inherited,
              job_ids=[j.job_id for j in jobs],precision_keys=sorted([list(k) for k in flags]),pressure_groups=sorted([list(k) for k in groups]),
              selected_keys=sorted([list(k) for k in keys]),new_batches=564,new_histories=28200,expected_points=243,expected_total_histories=92050,
              expected_history_counts=[dict(key=list(analysis.key(p)),ions=p['n_ions_total']+(600 if analysis.key(p) in keys else 0)) for p in analysis.points(rows)],
              planning_minutes=1.5*max(workers)/60+10,budget_hours=3,
              selection='All 35 remaining precision flags; plus all pressures in six groups with discovery mean spread >10% '
                        'in pressure×range or >5% in ionization fraction. These selection thresholds are not significance tests.',
              confirmation='Only new 600-ion cohorts enter independent pressure confirmation: twelve batches per pressure, '
                           'Welch ANOVA, BY correction over twelve fixed group/observable tests. No significance claims '
                           'until the family is complete. Non-rejection does not establish equivalence.',
              stop_rule='Fixed batches only. No repeated sampling until significance or precision passes; no grid expansion.')
    path=run/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan:raise ValueError('Frozen closeout plan changed')
    if not path.exists():write_json(path,plan)
    return plan,sr,jobs


def fresh_rows(run):
    directory=run/'confirmation'
    cfg=scan.load_config(CONFIG)
    rows=completed_rows(directory)[0]
    for row in rows:
        expected=scan.requested_target_energies(('C','F',row['family']),cfg)
        check=row.get('target_energy_check',{})
        if check.get('passed') is not True or check.get('expected')!=expected or check.get('observed')!=expected:
            raise ValueError('Unvalidated or different model in new report')
        row.update(model_id=MODEL,cohort='closeout',source_run=str(directory),raw_archive=str((directory/row['raw_archive']).resolve()),
                   normalized_profiles=str((directory/row['normalized_profiles']).resolve()))
    return rows


def independent_pressure(rows,groups):
    result=[]
    for f,ion,energy in sorted(groups):
        by_pressure={p:[r for r in rows if r['family']==f and r['ion']==ion and r['energy_kev']==energy
                       and r['noble_percent']==80 and r['pressure_atm']==p] for p in (.5,1.,3.)}
        if any(len(v)!=12 for v in by_pressure.values()):continue
        for field in analysis.FIELDS:
            values=[[r[field]*(p if field=='projected_range_mm' else 1) for r in batch] for p,batch in sorted(by_pressure.items())]
            means=[statistics.mean(v) for v in values]
            pvalue=None
            if all(statistics.variance(v)>0 for v in values):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',RuntimeWarning)
                    p=float(stats.f_oneway(*values,equal_var=False).pvalue)
                if math.isfinite(p):pvalue=p
            result.append(dict(family=f,ion=ion,energy_kev=energy,metric=field,model_id=MODEL,independent_batches_per_pressure=12,
                mean_p0p5=means[0],mean_p1=means[1],mean_p3=means[2],relative_mean_spread=(max(means)-min(means))/abs(statistics.mean(means)),
                pvalue=pvalue,adjusted_pvalue=None,flag='pending_family' if pvalue is not None else 'indeterminate'))
    if len(result)==2*len(groups):
        adjusted=stats.false_discovery_control([r['pvalue'] if r['pvalue'] is not None else 1. for r in result],method='by')
        for row,p in zip(result,adjusted):
            if row['pvalue'] is not None:row.update(adjusted_pvalue=float(p),flag='needs_investigation' if p<.05 else 'not_flagged')
    return result


def publish(run,state,final=False):
    plan=json.loads((run/'plan.json').read_text())
    old=previous.read_rows(previous.RUN);new=fresh_rows(run);rows=old+new
    if len({r['job_id'] for r in rows})!=len(rows) or len({r['random_seed'] for r in rows})!=len(rows):raise ValueError('Duplicated histories')
    points=analysis.points(rows);independent=analysis.points(new)
    expected={tuple(p['key']):p['ions'] for p in plan['expected_history_counts']}
    flags={tuple(k) for k in plan['precision_keys']}
    for row in points:
        row.update(model_id=MODEL,batches_expected=expected[analysis.key(row)]//50,all_batches_complete=row['n_ions_total']==expected[analysis.key(row)])
    for row in independent:row.update(model_id=MODEL,batches_expected=12,all_batches_complete=row['batches_complete']==12)
    pressure=independent_pressure(new,{tuple(g) for g in plan['pressure_groups']})
    summary=dict(expected_points=243,points_present=len(points),points_complete=sum(p['all_batches_complete'] for p in points),
        new_batches=len(new),expected_new_batches=564,total_histories=sum(r['n_ions'] for r in rows),
        points_meeting_estimated_precision=sum(p['all_batches_complete'] and p['precision_met'] for p in points),
        unresolved_precision_points=sum(not p['precision_met'] for p in points),
        independent_600ion_points_complete=sum(p['n_ions_total']==600 for p in independent),
        independent_600ion_points_meeting_targets=sum(p['n_ions_total']==600 and p['precision_met'] for p in independent),
        independent_selected35_meeting_targets=sum(p['n_ions_total']==600 and p['precision_met'] and analysis.key(p) in flags for p in independent),
        independent_pressure_tests=len(pressure),independent_pressure_flags=sum(p['flag'] in ('needs_investigation','indeterminate') for p in pressure))
    output=run/'report';output.mkdir(parents=True,exist_ok=True)
    for name,data in (('points',points),('batches',rows),('independent_new_points',independent),('pressure_confirmation',pressure),
                      ('precision_flagged',[p for p in points if not p['precision_met']])):
        atomic_text(output/f'{name}.csv',csv_text(data,sorted(set().union(*(r.keys() for r in data))) if data else ['no_rows']))
    write_json(run/'science_status.json',summary)
    if final:
        from matplotlib.backends.backend_pdf import PdfPages
        import matplotlib.pyplot as plt
        write_root(output/'scan.root',rows,points)
        with PdfPages(output/'summary.pdf') as pdf:
            for n in (60,80,95):
                fig=energy_figure(points,n,1);fig.suptitle(f'Corrected 1e-9 eV model · {n}% noble · batch SEM')
                fig.savefig(output/f'energy_{n}.png',dpi=120);pdf.savefig(fig);plt.close(fig)
            for axis in ('noble_percent','pressure_atm'):
                fig=scan_figure(points,axis);fig.savefig(output/f'{axis}.png',dpi=120);pdf.savefig(fig);plt.close(fig)
    body='''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>Corrected SRIM precision closeout</title>
<style>body{font:16px system-ui;max-width:1250px;margin:30px auto;padding:16px}table{border-collapse:collapse}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto;max-height:450px}img{max-width:100%}</style>
<h1>Fixed precision and independent pressure confirmation</h1>
<p>Same corrected 1e-9 eV model and 243-point grid; no grid expansion. 600 new ions at each of 47 points:
35 precision flags plus 12 additional controls covering six selected pressure groups. 564 new 50-ion batches,
distinct repeats/seeds; no legacy binding-model data or replay histories are pooled.</p>
<p>The previous 25 independent 600-ion cohorts all met precision targets. All 54 discovery pressure tests were unflagged,
but small cohorts had up to 20% spread in pressure×range or 7% in ionization fraction. Non-rejection is not equivalence.
Six groups were selected by spread thresholds, not significance. Independent confirmation uses only the new twelve
batches per pressure, with Welch ANOVA/BY correction across twelve fixed tests.</p>
<p>Estimated targets: range SEM ≤5%, ionization-fraction SEM ≤2%. Pooled post-selection SEMs are descriptive;
independent new cohorts are reported separately. SRIM ionization fraction is not detector-calibrated quenching.
No automatic additional batches, and no inference of untested mixture/pressure coverage.</p>
<p>15 cores, RAM 24/32 GiB soft/hard, zero scan swap, three cumulative hours.</p>
<p><a href="points.csv">Combined points</a> · <a href="independent_new_points.csv">Independent 600-ion estimates</a> ·
<a href="pressure_confirmation.csv">Independent pressure tests</a> · <a href="precision_flagged.csv">Precision flags</a> ·
<a href="batches.csv">Batch provenance/timings</a> · <a href="../plan.json">Frozen plan</a> · <a href="../logs/campaign.log">Log</a></p>'''
    body+=table([state],list(state))+table([summary],list(summary))+table(pressure,['family','ion','energy_kev','metric','relative_mean_spread','adjusted_pvalue','flag'])
    if (output/'summary.pdf').exists():
        body+='<p><a href="summary.pdf">PDF</a> · <a href="scan.root">ROOT</a>. Lines guide the eye; only grid points were simulated.</p>'
        body+=''.join(f'<img src="energy_{n}.png">' for n in (60,80,95))+'<img src="noble_percent.png"><img src="pressure_atm.png">'
    atomic_text(output/'index.html',body)
    return summary


class CloseoutCampaign(previous.RefinementCampaign):
    @property
    def budget_limit(self):return BUDGET
    def publish_report(self):return publish(self.run,self.state)

    def execute(self):
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,self.stop)
        try:
            group=Path(resource_sample()['cgroup'])
            if group.name!=UNIT or len(os.sched_getaffinity(0))>15 or (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0':raise RuntimeError('Use corrected_closeout.sh for resource limits')
            if memory_info()['MemAvailable']<16*GIB or self.used>=BUDGET:raise RuntimeError('Insufficient memory/time budget')
            plan,sr,jobs=prepare(self.run)
            source=scan.resolve_srim_root(None,scan.load_config(CONFIG))
            before=dict(controls=scan.source_control_fingerprint(source),data=scan.source_fingerprint(source))
            original=json.loads((previous.base.confirmation.pilot.RUN/'source_provenance.json').read_text())
            if original!=dict(source=before,execution=scan.execution_provenance(scan.resolve_execution_context(scan.load_config(CONFIG)))):raise RuntimeError('Validated source/execution changed')
            write_json(self.run/f'session-{os.getpid()}.json',dict(model_id=MODEL,source=before,cpu_affinity=sorted(os.sched_getaffinity(0)),
                 commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),started_at=self.state['started_at']))
            sources=[(previous.base.SOURCE,previous.base.SOURCE_CONFIG),(previous.base.RUN/'scan',previous.base.CONFIG)]
            sources.extend((previous.RUN/name,path) for name,path in previous.CONFIGS.items())
            for directory,path in sources:
                ids={r['job_id'] for r in completed_rows(directory)[0]}
                self.audit(directory,[j for j in configured_jobs(path)[1] if j.job_id in ids],path)
            self.state.update(status='RUNNING',phase='Read-only corrected baseline audit passed');self.update(force=True)
            self.selected('Selected stopping tables',CONFIG,self.run/'confirmation','srmodule',sr)
            self.selected('47 fixed points: twelve independent 50-ion batches each',CONFIG,self.run/'confirmation','trim',jobs)
            self.audit(self.run/'confirmation',jobs,CONFIG);prepare(self.run)
            events=dict(line.split() for line in (group/'memory.events').read_text().splitlines())
            if int(events.get('oom_kill',0)) or before!=dict(controls=scan.source_control_fingerprint(source),data=scan.source_fingerprint(source)):raise RuntimeError('OOM/source-integrity failure')
            summary=publish(self.run,self.state)
            if summary['points_complete']!=243 or summary['new_batches']!=564 or summary['total_histories']!=92050 or summary['independent_pressure_tests']!=12:raise RuntimeError('Final counts differ from frozen plan')
            self.state.update(status='REVIEW_REQUIRED',phase='Fixed precision/pressure confirmation complete',source_unchanged=True);self.update(force=True)
            summary=publish(self.run,self.state,final=True)
            write_json(self.run/'completion.json',dict(status='REVIEW_REQUIRED',summary=summary,resources=resource_sample()))
            return 0
        except Exception as exc:
            self.state.update(status='BUDGET_EXHAUSTED' if self.budget_stopped else 'STOPPED' if self.stopping else 'NEEDS_ATTENTION',error=str(exc))
            print(str(exc),file=sys.stderr,flush=True)
            if (self.run/'plan.json').exists():self.update(force=True)
            else:write_json(self.run/'status.json',self.state)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=30)
                except subprocess.TimeoutExpired:subprocess.run(['systemctl','--user','--no-block','stop',UNIT],check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('prepare','run','status'));args=parser.parse_args()
    if args.command=='status':
        unit=subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'],capture_output=True,text=True);print(unit.stdout.strip())
        if (RUN/'status.json').exists():
            state=json.loads((RUN/'status.json').read_text());print(json.dumps(state,indent=2))
            if state['status'] in ('RUNNING','STARTING') and ('ActiveState=active' not in unit.stdout or time.time()-datetime.fromisoformat(state['updated_at']).timestamp()>300):print('WARNING: inactive service or stale heartbeat')
        print(completed_rows(RUN/'confirmation')[1])
        if (RUN/'science_status.json').exists():print((RUN/'science_status.json').read_text())
        print('Report:',RUN/'report/index.html');return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,*_=prepare(RUN);print(json.dumps({k:v for k,v in plan.items() if k not in ('input_sha256','inherited_sha256','job_ids','selected_keys','precision_keys','expected_history_counts')},indent=2));return 0
        return CloseoutCampaign(RUN).execute()


if __name__=='__main__':raise SystemExit(main())
