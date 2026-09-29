#!/usr/bin/env python3
"""Fixed precision top-up, intermediate energies and limited pressure checks."""
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
import sqlite3
import statistics
import subprocess
import sys
import time
import warnings

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import srim_scan as scan
from scipy import stats
from scripts import corrected_scan as base
from scripts import precision_analysis as analysis
from scripts.precision_scan import job_key
from scripts.staged_scan import configured_jobs, write_json
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_report import write_root, energy_figure, scan_figure

RUN=ROOT/'runs/noble_cf4_corrected_refinement'
UNIT='srim-corrected-refinement.service'
COHORTS=('topup','energy','pressure')
CONFIGS={name:ROOT/f'configs/noble_cf4_corrected_{name}.toml' for name in COHORTS}
BUDGET=6*3600


def select_groups(rows, grids):
    points=analysis.points(rows)
    flagged=[p for p in points if not p['precision_met']]
    if len(points)!=108 or len(flagged)!=25 or any(p['n_ions_total']!=200 for p in flagged):
        raise ValueError('Expected reviewed 108 points and 25 flagged 200-ion points')
    keys={analysis.key(p) for p in flagged}
    groups={}
    old_seeds={r['random_seed'] for r in rows}
    seeds=set(old_seeds)
    for cohort,(stopping,candidates) in grids.items():
        jobs=[j for j in candidates if cohort!='topup' or job_key(j) in keys]
        expected={'topup':300,'energy':324,'pressure':216}[cohort]
        if len(jobs)!=expected:
            raise ValueError(f'{cohort}: unexpected selection size')
        for job in jobs:
            repeat=job.payload['repeat']
            if job.payload['n_ions']!=50 or repeat not in range(80,92 if cohort=='topup' else 84):
                raise ValueError('Unexpected batch size/repeat')
            seed=scan.seed_for(job.job_id,repeat)
            if seed in seeds:raise ValueError('Repeated history seed')
            seeds.add(seed)
        pairs={(j.payload['condition']['condition_id'],j.payload['ion']) for j in jobs}
        groups[cohort]=dict(jobs=jobs,stopping=[j for j in stopping if (j.payload['condition']['condition_id'],j.payload['ion']) in pairs])
    new_keys={job_key(j) for name,g in groups.items() if name!='topup' for j in g['jobs']}
    if len(new_keys)!=135 or new_keys & {analysis.key(r) for r in rows}:
        raise ValueError('New grid overlaps baseline or has wrong size')
    return groups, keys


def forecast(rows,groups):
    measured=defaultdict(list)
    for row in rows:measured[analysis.key(row)].append(row['wall_seconds_last_attempt'])
    legacy=defaultdict(list)
    for root in (ROOT/'runs/noble_cf4_staged/production',ROOT/'runs/noble_cf4_precision/refinement'):
        db=sqlite3.connect((root/'jobs.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
        try:records=db.execute("SELECT result_path,started_at,finished_at FROM jobs WHERE stage='trim' AND status='complete'").fetchall()
        finally:db.close()
        for path,start,end in records:
            row=json.loads((root/path).read_text())
            if row['n_ions']==50:
                legacy[analysis.key(row)].append((datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds())
    corrections={f:max(1.,max(statistics.mean(v)/statistics.mean(legacy[k]) for k,v in measured.items() if k[0]==f)) for f in ('Ar','Kr','Xe')}
    phase_hours={}
    for name,group in groups.items():
        costs=[]
        for job in group['jobs']:
            key=job_key(job)
            costs.append(statistics.mean(measured[key]) if key in measured else statistics.mean(legacy[key])*corrections[key[0]])
        workers=[0.]*15
        for cost in sorted(costs,reverse=True):heapq.heapreplace(workers,workers[0]+cost)
        phase_hours[name]=max(workers)/3600
    return dict(phase_model_hours=phase_hours,planning_hours=1.5*sum(phase_hours.values())+.5,
                family_corrections=corrections,assumptions='Measured 50-ion batch costs; historical times only for unsampled points; '
                'conservative family corrections, sequential 15-worker phases, 50% margin plus 30 minutes. Not guaranteed.')


def prepare(run):
    completion=json.loads((base.RUN/'completion.json').read_text())
    if completion['summary']['points_complete']!=108 or completion['summary']['complete_points_needing_precision']!=25:
        raise ValueError('Expected completed/reviewed coarse map')
    previous=json.loads((base.RUN/'plan.json').read_text())
    inherited={**previous['input_sha256'],**previous['inherited_sha256']}
    for path,digest in inherited.items():
        if scan.sha256_file(ROOT/path)!=digest:raise ValueError('Frozen baseline inputs changed')
    rows=base.combined_rows(base.RUN)
    grids={name:configured_jobs(path) for name,path in CONFIGS.items()}
    groups,keys=select_groups(rows,grids)
    physics=scan.load_config(base.CONFIG).raw['physics']
    for path in CONFIGS.values():
        cfg=scan.load_config(path)
        if cfg.raw['physics']!=physics or cfg.temperature_k!=293.15 or cfg.energy_min_kev!=1 or cfg.energy_max_kev!=1000:
            raise ValueError('Changed model or stopping-table bounds')
    paths=[Path(__file__),ROOT/'scripts/corrected_refinement.sh',*CONFIGS.values(),
           base.RUN/'plan.json',base.RUN/'completion.json',base.RUN/'report/points.csv',base.RUN/'report/precision_flagged.csv']
    for row in rows:
        paths.extend((Path(row['raw_archive']),Path(row['normalized_profiles']),Path(row['source_run'])/'job_results/trim'/f"{row['job_id']}.json"))
    plan=dict(model_id=base.MODEL,input_sha256={str(p.relative_to(ROOT)):scan.sha256_file(p) for p in paths}, inherited_sha256=inherited,
              job_ids={k:[j.job_id for j in g['jobs']] for k,g in groups.items()},flagged_keys=sorted([list(k) for k in keys]),
              new_batches=840,new_histories=42000,expected_combined_points=243,expected_total_histories=63850,
              topup='25 fixed points: 200 + 600 = 800 ions each, repeats 80-91. Independent new-cohort estimates reported separately.',
              energy='81 new points: Ar/Kr/Xe, 60/80/95%, 1 atm, C/F/noble, 3/30/300 keV; 200 ions each.',
              pressure='54 new points: Ar/Kr/Xe, 80%, 0.5/3 atm, C/F/noble, 1/10/100 keV; 200 ions each.',
              pressure_diagnostics='Fixed first four batches per point at 0.5/1/3 atm; Welch ANOVA, BY adjustment over '
              '54 point/observable tests on pressure-times-range and ionization fraction. Small-cohort diagnostic; '
              'non-rejection does not establish equivalence. No adaptive extra simulation.',
              budget_hours=6,forecast=forecast(rows,groups),
              exclusions='No different binding models, no 1 MeV pressure extension, no new concentrations, no automatic top-ups.')
    path=run/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan:raise ValueError('Frozen refinement plan changed')
    if not path.exists():write_json(path,plan)
    return plan,groups,keys


def read_rows(run):
    rows=base.combined_rows(base.RUN)
    for row in rows:row['cohort']='baseline'
    for name in COHORTS:
        directory=run/name
        for row in completed_rows(directory)[0]:
            expected=scan.requested_target_energies(('C','F',row['family']),scan.load_config(CONFIGS[name]))
            check=row.get('target_energy_check',{})
            if check.get('passed') is not True or check.get('expected')!=expected or check.get('observed')!=expected:
                raise ValueError('Unchecked or different binding model in new report')
            row.update(cohort=name,model_id=base.MODEL,source_run=str(directory),
                       raw_archive=str((directory/row['raw_archive']).resolve()),
                       normalized_profiles=str((directory/row['normalized_profiles']).resolve()))
            rows.append(row)
    if len({r['job_id'] for r in rows})!=len(rows) or len({r['random_seed'] for r in rows})!=len(rows):
        raise ValueError('Repeated histories in combined report')
    return rows


def pressure_checks(rows):
    groups=defaultdict(lambda:defaultdict(list))
    for row in rows:
        if row['noble_percent']==80 and row['energy_kev'] in (1,10,100) and row['pressure_atm'] in (.5,1,3):
            groups[row['family'],row['ion'],row['energy_kev']][row['pressure_atm']].append(row)
    results=[]
    for key,pressures in sorted(groups.items()):
        if set(pressures)!={.5,1,3} or any(len(v)<4 for v in pressures.values()):continue
        for field in analysis.FIELDS:
            values=[[r[field]*(p if field=='projected_range_mm' else 1) for r in sorted(batch,key=lambda r:r['repeat'])[:4]]
                    for p,batch in sorted(pressures.items())]
            means=[statistics.mean(v) for v in values]
            pvalue=None
            if all(statistics.variance(v)>0 for v in values):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',RuntimeWarning)
                    p=float(stats.f_oneway(*values,equal_var=False).pvalue)
                if math.isfinite(p):pvalue=p
            results.append(dict(family=key[0],ion=key[1],energy_kev=key[2],metric=field,
                pressure_values='0.5;1;3',batch_count_per_pressure=4,
                relative_mean_spread=(max(means)-min(means))/abs(statistics.mean(means)),
                pvalue=pvalue,adjusted_pvalue=None,flag='pending_family' if pvalue is not None else 'indeterminate'))
    # Do not change the multiple-testing family as results arrive.
    if len(results)==54:
        # Keep all 54 slots even if a zero-variance test cannot be estimated.
        adjusted=stats.false_discovery_control([r['pvalue'] if r['pvalue'] is not None else 1. for r in results],method='by')
        for row,p in zip(results,adjusted):
            if row['pvalue'] is not None:
                row.update(adjusted_pvalue=float(p),flag='needs_investigation' if p<.05 else 'not_flagged')
    return results


def publish(run,state,final=False):
    rows=read_rows(run)
    points=analysis.points(rows)
    independent=analysis.points([r for r in rows if r['cohort']=='topup'])
    pressure=pressure_checks(rows)
    baseline_points=analysis.points([r for r in rows if r['cohort']=='baseline'])
    expected={analysis.key(p):p['batches_complete']+(12 if not p['precision_met'] else 0) for p in baseline_points}
    for p in points:
        p.update(model_id=base.MODEL,batches_expected=expected.get(analysis.key(p),4))
        p['all_batches_complete']=p['batches_complete']==p['batches_expected']
    for p in independent:
        p.update(model_id=base.MODEL,batches_expected=12,all_batches_complete=p['batches_complete']==12)
    for p in pressure:p['model_id']=base.MODEL
    new=[r for r in rows if r['cohort']!='baseline']
    summary=dict(model_id=base.MODEL,expected_points=243,points_present=len(points),new_batches=len(new),expected_new_batches=840,
                 total_histories=sum(r['n_ions'] for r in rows),points_at_800_ions=sum(r['n_ions_total']==800 for r in points),
                 points_complete=sum(r['all_batches_complete'] for r in points),
                 points_meeting_estimated_precision=sum(r['all_batches_complete'] and r['precision_met'] for r in points),
                 unresolved_precision_points=sum(not r['precision_met'] for r in points),
                 independent_600ion_points_complete=sum(r['n_ions_total']==600 for r in independent),
                 independent_600ion_points_meeting_targets=sum(r['n_ions_total']==600 and r['precision_met'] for r in independent),
                 pressure_tests_complete=len(pressure),pressure_flags=sum(r['flag'] in ('needs_investigation','indeterminate') for r in pressure))
    output=run/'report';output.mkdir(parents=True,exist_ok=True)
    for name,data in (('points',points),('batches',rows),('independent_topup',independent),('pressure_checks',pressure),
                      ('precision_flagged',[r for r in points if not r['precision_met']])):
        atomic_text(output/f'{name}.csv',csv_text(data,sorted(set().union(*(r.keys() for r in data))) if data else ['no_rows']))
    write_json(run/'science_status.json',summary)
    if final:
        from matplotlib.backends.backend_pdf import PdfPages
        import matplotlib.pyplot as plt
        write_root(output/'scan.root',rows,points)
        with PdfPages(output/'summary.pdf') as pdf:
            for mixture in (60,80,95):
                fig=energy_figure(points,mixture,1)
                fig.suptitle(f'Corrected 1e-9 eV model · {mixture}% noble · batch SEM, not model uncertainty')
                fig.savefig(output/f'energy_{mixture}.png',dpi=120);pdf.savefig(fig);plt.close(fig)
            for axis in ('noble_percent','pressure_atm'):
                fig=scan_figure(points,axis)
                fig.savefig(output/f'{axis}.png',dpi=120);pdf.savefig(fig);plt.close(fig)
    body='''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Corrected SRIM refinement</title><style>body{font:16px system-ui;max-width:1250px;margin:30px auto;padding:16px}
table{border-collapse:collapse}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto;max-height:450px}img{max-width:100%}</style>
<h1>Corrected Ar/Kr/Xe–CF4: precision, energies and pressure</h1>
<p>Model noble-cf4-nearzero-1e9-v1. Previous corrected 108-point map is read-only.
25 fixed points receive 600 fresh ions (200 → 800); their independent new-cohort estimates are separate.
Add 3/30/300 keV at 60/80/95% noble and 1 atm (81 points), plus 0.5/3 atm at 80% noble,
1/10/100 keV (54 points). All new points have four 50-ion batches. Expected combined grid: 243 direct points.
No 1 MeV pressure extension or other concentrations are simulated.</p>
<p>Estimated SEM targets: range 5%, ionization fraction 2%. Pooled estimates after point selection are descriptive,
not guaranteed interval coverage or detector-calibrated quenching. No automatic extra batches.</p>
<p>Pressure diagnostics use the fixed first four batches per pressure, not selected top-ups: Welch ANOVA with BY
adjustment after all 54 tests are available. Compare pressure×range and ionization fraction.
Non-rejection does not prove pressure equivalence; small batch counts limit sensitivity.</p>
<p>15 cores; RAM 24/32 GiB soft/hard; zero scan swap; six cumulative hours.</p>
<p><a href="points.csv">Combined point CSV</a> · <a href="precision_flagged.csv">Precision flags</a> ·
<a href="independent_topup.csv">Independent 600-ion cohorts</a> · <a href="pressure_checks.csv">Pressure checks</a> ·
<a href="batches.csv">Batches/provenance/timing</a> · <a href="../plan.json">Frozen plan</a> · <a href="../logs/campaign.log">Logs</a></p>'''
    body+=table([state],list(state))+table([summary],list(summary))
    if (output/'summary.pdf').exists():
        body+='<p><a href="summary.pdf">Final PDF</a> · <a href="scan.root">ROOT file</a>. Plot lines guide the eye, not simulated intermediate values.</p>'
        body+=''.join(f'<img src="energy_{n}.png">' for n in (60,80,95))+'<img src="noble_percent.png"><img src="pressure_atm.png">'
    body+=table([r for r in points if not r['precision_met']],['family','noble_percent','pressure_atm','ion','energy_kev','n_ions_total','projected_range_mm_relative_sem','qf_trim_ionization_relative_sem'])
    atomic_text(output/'index.html',body)
    return summary


class RefinementCampaign(base.CorrectedCampaign):
    @property
    def budget_limit(self):return BUDGET

    def publish_report(self):return publish(self.run,self.state)

    def execute(self):
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,self.stop)
        try:
            group=Path(resource_sample()['cgroup'])
            if (group.name!=UNIT or len(os.sched_getaffinity(0))>15 or (group/'memory.max').read_text().strip()!=str(32*GIB)
                    or (group/'memory.swap.max').read_text().strip()!='0'):
                raise RuntimeError('Use corrected_refinement.sh for enforced limits')
            if memory_info()['MemAvailable']<16*GIB or self.used>=BUDGET:raise RuntimeError('Insufficient memory or time budget')
            plan,groups,keys=prepare(self.run)
            source=scan.resolve_srim_root(None,scan.load_config(CONFIGS['topup']))
            before=dict(controls=scan.source_control_fingerprint(source),data=scan.source_fingerprint(source))
            original=json.loads((base.confirmation.pilot.RUN/'source_provenance.json').read_text())
            if original!=dict(source=before,execution=scan.execution_provenance(scan.resolve_execution_context(scan.load_config(CONFIGS['topup'])))):
                raise RuntimeError('SRIM/PySRIM installation differs from validated source')
            write_json(self.run/f'session-{os.getpid()}.json',dict(model_id=base.MODEL,source=before,
                commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                cpu_affinity=sorted(os.sched_getaffinity(0)),started_at=self.state['started_at']))
            for source_run,config in ((base.SOURCE,base.SOURCE_CONFIG),(base.RUN/'scan',base.CONFIG)):
                ids={r['job_id'] for r in completed_rows(source_run)[0]}
                jobs=[j for j in configured_jobs(config)[1] if j.job_id in ids]
                self.audit(source_run,jobs,config)
            self.state.update(status='RUNNING',phase='Corrected baseline audited')
            self.update(force=True)
            for name,g in groups.items():
                self.selected(name+' stopping tables',CONFIGS[name],self.run/name,'srmodule',g['stopping'])
                self.selected(name+f": {len(g['jobs'])} fixed independent batches",CONFIGS[name],self.run/name,'trim',g['jobs'])
                self.audit(self.run/name,g['jobs'],CONFIGS[name])
                prepare(self.run)
                events=dict(line.split() for line in (group/'memory.events').read_text().splitlines())
                unchanged=before==dict(controls=scan.source_control_fingerprint(source),data=scan.source_fingerprint(source))
                passed=unchanged and int(events.get('oom_kill',0))==0 and memory_info()['MemAvailable']>=8*GIB
                write_json(self.run/f'{name}_validation.json',dict(passed=passed,batches=len(g['jobs']),source_unchanged=unchanged,memory_events=events))
                if not passed:raise RuntimeError(name+' resource/source gate failed; no subsequent stage released')
            summary=publish(self.run,self.state)
            if (summary['points_present']!=243 or summary['points_complete']!=243 or summary['new_batches']!=840 or summary['total_histories']!=63850
                    or summary['points_at_800_ions']!=25 or summary['pressure_tests_complete']!=54):
                raise RuntimeError('Final grid/history counts differ from frozen plan')
            self.state.update(status='REVIEW_REQUIRED',phase='Fixed corrected-model refinement complete',source_unchanged=True)
            self.update(force=True)
            summary=publish(self.run,self.state,final=True)
            write_json(self.run/'completion.json',dict(status='REVIEW_REQUIRED',summary=summary,resources=resource_sample()))
            return 0
        except Exception as exc:
            self.state.update(status='BUDGET_EXHAUSTED' if self.budget_stopped else 'STOPPED' if self.stopping else 'NEEDS_ATTENTION',error=str(exc))
            print(str(exc),file=sys.stderr,flush=True)
            self.update(force=True)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=30)
                except subprocess.TimeoutExpired:subprocess.run(['systemctl','--user','--no-block','stop',UNIT],check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('prepare','run','status'));args=parser.parse_args()
    if args.command=='status':
        unit=subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'],capture_output=True,text=True)
        print(unit.stdout.strip())
        if (RUN/'status.json').exists():
            state=json.loads((RUN/'status.json').read_text());print(json.dumps(state,indent=2))
            if state['status'] in ('RUNNING','STARTING') and ('ActiveState=active' not in unit.stdout or time.time()-datetime.fromisoformat(state['updated_at']).timestamp()>300):print('WARNING: inactive service or stale heartbeat; inspect logs')
        for name in COHORTS:print(name,completed_rows(RUN/name)[1])
        if (RUN/'science_status.json').exists():print((RUN/'science_status.json').read_text())
        print('Report:',RUN/'report/index.html');return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,*_=prepare(RUN);print(json.dumps({k:v for k,v in plan.items() if k not in ('input_sha256','inherited_sha256','job_ids','flagged_keys')},indent=2));return 0
        return RefinementCampaign(RUN).execute()


if __name__=='__main__':raise SystemExit(main())
