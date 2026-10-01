#!/usr/bin/env python3
"""C3b: fixed bulk baseline rebuild with paired-depth release gates; no chaining."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from datetime import datetime
import fcntl
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import review_bulk_pilot as previous
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample,memory_info,GIB
p=previous.pilot
base,scan=p.base,p.scan
RUN=ROOT/'runs/noble_cf4_bulk_baseline'
UNIT='srim-bulk-baseline.service'
MODEL=p.MODEL
WORKERS=15
BUDGET=12*3600
JOB_TIMEOUT=3*3600
ENERGIES=(1.,3.,10.,30.,100.,300.,1000.)
PRESSURE_SPEC=dict(comparisons=108,familywise_alpha=.05,ionization_ratio_margin=[.95,1.05],
                   pressure_scaled_range_ratio_margin=[.90,1.10],
                   method='Approximate Welch intervals for differences of mean log batch observables, '
                   'Bonferroni 108 two-sided comparisons: 9 family/recoil combinations x 3 energies '
                   '(1/10/100keV) x 2 pressures (0.5/3 versus 1atm) x 2 observables. '
                   'Range is multiplied by pressure before logs. Exponentiated intervals describe ratios '
                   'of geometric batch means, not arithmetic means. Four independent batches per condition. '
                   'Inside margin = locally compatible at these working margins; outside = follow-up; '
                   'overlap = inconclusive. Zero variance is unresolved. Approximate normal log-batch '
                   'assumption, not exact coverage with n=4, global equivalence or model accuracy. '
                   'Existing 1atm pilot observations are already visible; this is a prospective screening '
                   'plan for new pressure data, not an independent confirmation of pilot-selected effects.')


def key(r):return (r['family'],r['ion'],float(r['energy_kev']),int(r['noble_percent']),float(r['pressure_atm']))


def name_for(k):
    f,ion,e,n,pressure=k
    return f'{f}_{ion}_e{e:g}_n{n}_p{pressure:g}'


def make_input(original,name,seed,z,energy,factor=2):
    if energy not in ENERGIES or z not in (6,9,18,36,54) or factor not in (2,4) or not 0<seed<900000001:
        raise ValueError('Unexpected rebuild parameters')
    lines=original.decode('ascii').splitlines()
    ion=lines[next(i for i,x in enumerate(lines) if x.startswith('Ion:'))+1].split()
    if int(ion[0])!=z or float(ion[2])!=energy or int(ion[4])!=50:raise ValueError('Source primary mismatch')
    index=next(i for i,x in enumerate(lines) if x.startswith('1 "'))
    layer=shlex.split(lines[index]);width=float(layer[2])*factor;origin=width/2
    lines[index]=f'1 "{layer[1]}" {width:.17g} '+' '.join(layer[3:])
    index=next(i for i,x in enumerate(lines) if x.startswith('Cascades('))+1
    fields=lines[index].split();fields[:2]=['5',str(seed)];lines[index]=' '.join(fields)
    index=next(i for i,x in enumerate(lines) if x.startswith('Diskfiles'))+1
    fields=lines[index].split();fields[0]='1';lines[index]=' '.join(fields)
    index=next(i for i,x in enumerate(lines) if x.startswith('PlotType'))+1
    lines[index]=f'5 0.0 {width:.17g}'
    lines[lines.index('Target material : Number of Elements, Number of Layers')+1]=f'"{name}" 3 1'
    header=['TRIM.DAT bulk baseline rebuild','Ten header lines; CRLF and DOS EOF',
            'Full cascade mode 5; central start','Exactly fifty primaries per independent batch',
            'Energy in eV; coordinates in Angstrom','Direction +X; Y=Z=0','Five-character event identity',
            f'{name} Z={z} E={energy:g}keV','Event Z Energy X Y Z CosX CosY CosZ','BEGIN EVENTS AFTER TENTH LINE']
    events=[f'E{i:04d} {z} {energy*1000:.17g} {origin:.17g} 0 0 1 0 0' for i in range(1,51)]
    return ('\r\n'.join(lines)+'\r\n').encode('ascii'),('\r\n'.join(header+events)+'\r\n').encode('ascii')+b'\x1a',origin,width


def calibrated_timings(timing):
    old=base.schedule.timing_means(base.schedule.TIMINGS)
    measured={(r['family'],r['ion'],r['energy_kev']):r['mean_seconds'] for r in timing}
    means={}
    for k,value in old.items():
        f,ion,e,n,pressure=k
        ratio=max(1.,*(measured[f,ion,ee]/old[f,ion,ee,80,1.] for ee in (100.,1000.)))
        overhead=max(0.,measured[f,ion,1.]-old[f,ion,1.,80,1.])
        means[k]=value*ratio+overhead
    return means


def prepare(run):
    review,retained,_,timing=previous.review()
    source_rows=base.d.prior.previous.read_rows(base.d.prior.previous.RUN)+base.d.prior.fresh_rows(base.d.prior.RUN)
    records={}
    for r in sorted(source_rows,key=lambda r:r['repeat']):records.setdefault(key(r),r)
    if len(records)!=243:raise ValueError('Expected 243 source points')
    retained_keys={key(r) for r in retained};selected=set(records)-retained_keys
    # All species at low-energy mixture/pressure boundaries, plus heavy high-energy mixture boundaries.
    controls={k for k in selected if k[2]==1 or (k[2]==1000 and k[1]==k[0] and k[3] in (60,95))}
    if len(selected)!=216 or len(controls)!=42:raise ValueError('Rebuild/control selection changed')
    diagnostics=json.loads((p.previous.RUN/'results.json').read_text())
    used={r['random_seed'] for r in source_rows}|{r['seed'] for r in retained+diagnostics}
    means=calibrated_timings(timing);cases=[];inputs={}
    paths={Path(__file__),ROOT/'scripts/bulk_baseline_rebuild.sh',Path(previous.__file__),Path(p.__file__),
           previous.RUN/'plan.json',previous.RUN/'completion.json',previous.RUN/'results.json',base.schedule.TIMINGS}
    for r in retained:
        paths.add(previous.RUN/'cases'/r['name']/'result.json');paths.update(Path(a) for a in r['artifact_sha256'])
    for k in sorted(selected):
        row=records[k];point=name_for(k);source=Path(row['raw_archive']);paths.add(source)
        for repeat in range(4):
            seed=scan.seed_for(MODEL+'-rebuild-'+point,repeat)
            if seed in used:raise ValueError('Seed collision')
            used.add(seed)
            factors=(2,4) if repeat==0 and k in controls else (2,)
            for factor in factors:
                diagnostic=factor==4;name=f'{point}_b{repeat}'+('_deep' if diagnostic else '')
                content,events,origin,width=make_input(base.d.archive_data(source,'TRIM.IN'),name,seed,row['ion_z'],k[2],factor)
                inputs[name]=(content,events)
                phase=('low_geometry' if k[2]==1 else 'high_geometry') if repeat==0 and k in controls else 'baseline'
                cases.append(dict(name=name,point=point,repeat=repeat,phase=phase,diagnostic=diagnostic,
                                  family=k[0],ion=k[1],ion_z=row['ion_z'],energy_kev=k[2],noble_percent=k[3],pressure_atm=k[4],
                                  geometry='interior_deep' if diagnostic else 'interior',model_id=MODEL,ions=50,seed=seed,
                                  source=str(source),origin_angstrom=origin,width_angstrom=width,
                                  expected=row['target_energy_check']['expected'],
                                  input_sha256=hashlib.sha256(content).hexdigest(),trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                                  estimated_seconds=means[k]))
    phases={}
    for phase in ('low_geometry','high_geometry','baseline'):
        loads=[0.]*WORKERS
        for cost in sorted((c['estimated_seconds'] for c in cases if c['phase']==phase),reverse=True):heapq.heapreplace(loads,loads[0]+cost)
        phases[phase]=max(loads)/3600
    forecast=dict(model_hours=sum(phases.values()),planning_hours=1.5*sum(phases.values())+.5,
                  phase_model_hours=phases,longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases)/3600,
                  method='Measured old-grid times scaled upward by the larger observed bulk/surface ratio at 100/1000keV '
                  'and nonnegative 1keV startup overhead. 15-worker longest-first scheduling within three sequential gates; '
                  '50% margin plus 30 minutes. Mixture/pressure extrapolation is planning, not physics.')
    if len(cases)!=906 or forecast['planning_hours']>BUDGET/3600 or forecast['longest_estimated_batch_hours']>2.5:
        raise ValueError(f'Case count {len(cases)} or forecast does not fit cap with drain allowance: {forecast}')
    plan=dict(stage='C3b',model_id=MODEL,cases=cases,total_cases=906,new_points=216,retained_points=27,
              independent_new_batches=864,diagnostic_batches=42,new_independent_histories=43200,diagnostic_histories=2100,
              combined_points=243,combined_independent_batches=972,workers=WORKERS,budget_hours=12,
              job_timeout_seconds=JOB_TIMEOUT,forecast=forecast,previous_review=review,
              pressure_screen=PRESSURE_SPEC,depth_margins=dict(ionization_relative=.002,range_relative=.005),
              phases='72 low-energy paired cases, then 12 heavy high-energy paired cases, then remaining 822 independent batches. '
              'Each complete gate must pass before release. Forty-two deep cases share seeds with baseline b0 and are excluded from statistics.',
              scope='216 new points plus 27 audited pilot points = 243. 60/80/95% noble, 1-1000keV seven-energy grid at 1atm; '
              '80% noble at 0.5/3atm and 1/10/100keV. Four independent 50-ion batches per point. '
              'Unchanged mode5 bulk model; no surface results reused. Preserve all five pilot precision flags. No automatic top-ups.',
              gates='Exact input/hash/actual parameter/completion checks, all fifty endpoints, zero escape/edge signal. '
              'Paired-depth ionization changes <=0.2% and displacement <=0.5%; numerical screens, not statistical equivalence. '
              'Final precision and pressure flags require review, not automatic resampling or next-stage launch.',
              input_sha256={str(path):scan.sha256_file(path) for path in sorted(paths)})
    target=run/'plan.json'
    if target.exists() and json.loads(target.read_text())!=plan:raise ValueError('Frozen rebuild plan changed')
    if not target.exists():write_json(target,plan)
    return plan,inputs,retained


def production_rows(retained,rows):return retained+[r for r in rows if not r['diagnostic']]


def run_case(case,contents,runtime,context,deadline):
    (runtime/'.display-startup.json').unlink(missing_ok=True)
    (runtime/'.trim-process.log').unlink(missing_ok=True)
    result=p.prior.old_run_case(case,contents,runtime,context,deadline)
    metadata=runtime/'.display-startup.json'
    if metadata.exists():
        target=Path(result['attempt'])/metadata.name;shutil.copy2(metadata,target)
        result['artifact_sha256'][str(target)]=scan.sha256_file(target)
        result['display_startup']=json.loads(target.read_text())
    if result['status']=='COMPLETE' and result.get('display_startup',{}).get('allocation')!='Xvfb -displayfd':
        result.update(status='FAILED',error='Missing atomic display evidence')
    write_json(RUN/'cases'/case['name']/'result.json',result)
    return result


def geometry_checks(rows):
    selected=[r for r in rows if r['phase'] in ('low_geometry','high_geometry')]
    return base.depth_checks(selected)


def pressure_checks(rows):
    from scipy.stats import t
    groups={}
    for r in rows:
        if r['status']=='COMPLETE' and r['noble_percent']==80 and r['energy_kev'] in (1,10,100):
            groups.setdefault(key(r),[]).append(r)
    result=[]
    for f in ('Ar','Kr','Xe'):
        for ion in ('C','F',f):
            for e in (1.,10.,100.):
                reference=groups.get((f,ion,e,80,1.),[])
                for pressure in (.5,3.):
                    rr=groups.get((f,ion,e,80,pressure),[])
                    if len(reference)!=4 or len(rr)!=4:continue
                    for field,margins in [('ionization_fraction',(.95,1.05)),('stopped_ion_mean_dx_mm',(.90,1.10))]:
                        def values(batch):
                            return [math.log(r[field]*(r['pressure_atm'] if field=='stopped_ion_mean_dx_mm' else 1)) for r in batch]
                        a,b=values(reference),values(rr);va,vb=statistics.variance(a)/4,statistics.variance(b)/4
                        delta=statistics.mean(b)-statistics.mean(a)
                        row=dict(family=f,ion=ion,energy_kev=e,pressure_atm=pressure,metric=field,ratio=math.exp(delta),
                                 margin_low=margins[0],margin_high=margins[1],classification='inconclusive_zero_variance')
                        if va+vb>0:
                            df=(va+vb)**2/(va*va/3+vb*vb/3)
                            half=float(t.ppf(1-.05/(2*108),df))*math.sqrt(va+vb)
                            low,high=math.exp(delta-half),math.exp(delta+half)
                            label='within_working_margin' if low>=margins[0] and high<=margins[1] else 'followup' if high<margins[0] or low>margins[1] else 'inconclusive'
                            row.update(ratio_low=low,ratio_high=high,welch_df=df,classification=label)
                        result.append(row)
    return result


def publish(state,rows,retained):
    combined=production_rows(retained,rows);points=p.point_summary(combined);checks=geometry_checks(rows)
    state.update(new_cases_complete=sum(r['status']=='COMPLETE' for r in rows),expected_new_cases=906,
                 combined_points_complete=sum(r['complete'] for r in points),combined_points_expected=243,
                 points_meeting_precision=sum(r['precision_met'] for r in points),
                 completed_points_flagged=sum(r['complete'] and not r['precision_met'] for r in points),
                 depth_pairs_complete=len(checks),depth_pairs_passed=sum(r['passed'] for r in checks))
    for filename,data in [('status',state),('results',rows),('points',points),('depth_checks',checks)]:write_json(RUN/(filename+'.json'),data)
    atomic_text(RUN/'points.csv',csv_text(points,list(points[0]) if points else []))
    columns=['name','status','model_id','family','ion','energy_kev','noble_percent','pressure_atm','repeat','seed',
             'ions_confirmed','stopped_ion_mean_dx_mm','ionization_fraction','wall_seconds','attempt']
    atomic_text(RUN/'batches.csv',csv_text(combined,columns))
    body='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>Bulk baseline rebuild</title>'
    body+='<h1>C3b: compatible bulk baseline</h1><p>27 retained pilot points plus 216 new points; 864 new independent batches, '
    body+='42 separate paired-depth diagnostics. No surface results reused. At most 15 cores; 24/32 GiB RAM; no scan swap; 12-hour cap.</p>'
    body+='<p>Range is mean endpoint X minus start X, not track length. Ionization fraction is not detector-calibrated quenching. '
    body+='Four-batch SEM targets: range 5%, ionization 2%. Fixed samples; flags do not trigger automatic top-ups.</p>'
    body+='<p><a href="points.csv">Points CSV</a> · <a href="batches.csv">Independent batches CSV</a> · '
    body+='<a href="plan.json">Frozen design and pressure-screen specification</a> · <a href="logs/campaign.log">Log</a> · '
    body+='<a href="../noble_cf4_bulk_precision_pilot/review/index.html">Audited pilot</a></p>'
    body+=table([state],list(state))
    if points:body+=table(points,list(points[0]))
    if checks:body+='<h2>Paired-depth gates (diagnostics excluded from statistics)</h2>'+table(checks,list(checks[0]))
    if (RUN/'pressure_checks.json').exists():body+='<p><a href="pressure_checks.csv">Prespecified pressure screen</a></p>'
    atomic_text(RUN/'index.html',body)


def execute():
    group=Path(resource_sample()['cgroup'])
    if (group.name!=UNIT or len(os.sched_getaffinity(0))>WORKERS or
        (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0'):
        raise RuntimeError('Use bulk_baseline_rebuild.sh for enforced resource limits')
    used=json.loads((RUN/'budget.json').read_text())['used_seconds'] if (RUN/'budget.json').exists() else 0.
    if used>=BUDGET or memory_info()['MemAvailable']<16*GIB:raise RuntimeError('Insufficient time/memory budget')
    if (RUN/'completion.json').exists():raise RuntimeError('Already complete; review instead of relaunching')
    started=time.monotonic();deadline=started+BUDGET-used;rows=[];retained=[]
    state=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),phase='Preparing audited baseline',model_id=MODEL)
    def stop(sig,frame):scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stop)
    def update():
        elapsed=used+time.monotonic()-started
        if elapsed>=BUDGET:scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(),cumulative_hours=elapsed/3600,resources=resource_sample())
        write_json(RUN/'budget.json',dict(used_seconds=elapsed,limit_seconds=BUDGET));publish(state,rows,retained)
    try:
        update();plan,inputs,retained=prepare(RUN)
        context=scan.resolve_execution_context(scan.load_config(base.d.CONFIG))
        runtimes,_=scan.prepare_runtimes(scan.resolve_srim_root(None,scan.load_config(base.d.CONFIG)),RUN,WORKERS,context)
        write_json(RUN/f'session-{os.getpid()}.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                                                        cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase,expected_pairs in [('low_geometry',36),('high_geometry',42),('baseline',42)]:
            state['phase']=phase;pending=[]
            for case in plan['cases']:
                if case['phase']!=phase:continue
                cached=base.cached(case)
                if cached:
                    previous.audit_case(case,cached,inputs[case['name']]);rows.append(cached)
                else:pending.append(case)
            pending.sort(key=lambda c:c['estimated_seconds'],reverse=True)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                available,active=list(runtimes),{}
                while pending or active:
                    while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline-time.monotonic()<JOB_TIMEOUT+90:scan.SHUTDOWN_REQUESTED.set();break
                        case,runtime=pending.pop(0),available.pop()
                        active[pool.submit(run_case,case,inputs[case['name']],runtime,
                                           scan.runtime_execution_context(context,RUN,runtime),deadline)]=runtime
                    if not active:break
                    done,_=wait(active,timeout=5,return_when=FIRST_COMPLETED)
                    for future in done:
                        available.append(active.pop(future))
                        try:r=future.result()
                        except Exception:scan.SHUTDOWN_REQUESTED.set();raise
                        rows.append(r);print(json.dumps({k:v for k,v in r.items() if k!='artifact_sha256'}),flush=True)
                        if r['status']!='COMPLETE':scan.SHUTDOWN_REQUESTED.set()
                    if any(not c['passed'] for c in geometry_checks(rows)):scan.SHUTDOWN_REQUESTED.set()
                    update()
            if any(r['status']=='FAILED' for r in rows) or any(not c['passed'] for c in geometry_checks(rows)):
                raise RuntimeError('Integrity or paired-depth failure; dependent work held')
            if scan.SHUTDOWN_REQUESTED.is_set():raise InterruptedError('Stopped or insufficient cumulative budget')
            if len(geometry_checks(rows))!=expected_pairs:raise ValueError('Incomplete geometry gate')
            update()
        prepare(RUN)
        indexed={r['name']:r for r in rows}
        if len(rows)!=906 or len(indexed)!=906:raise ValueError('Incomplete or duplicate rebuild cases')
        for case in plan['cases']:previous.audit_case(case,indexed[case['name']],inputs[case['name']])
        combined=production_rows(retained,rows);points=p.point_summary(combined)
        if len(points)!=243 or not all(x['complete'] for x in points):raise ValueError('Incomplete bulk baseline')
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill',0)):
            raise RuntimeError('OOM observed')
        pressure=pressure_checks(combined)
        if len(pressure)!=108:raise ValueError('Incomplete pressure screening family')
        write_json(RUN/'pressure_checks.json',pressure)
        atomic_text(RUN/'pressure_checks.csv',csv_text(pressure,sorted({k for r in pressure for k in r})))
        state.update(status='REVIEW_REQUIRED',phase='Bulk baseline complete; review precision and pressure before P2a',source_unchanged=True,
                     pressure_followups=sum(r['classification']=='followup' for r in pressure),
                     pressure_inconclusive=sum(r['classification'].startswith('inconclusive') for r in pressure))
        update();write_json(RUN/'completion.json',state);return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc,InterruptedError) else 'NEEDS_ATTENTION',error=str(exc))
        update();print(str(exc),file=sys.stderr,flush=True);return 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('prepare','run','status'))
    args=parser.parse_args()
    if args.command=='status':
        subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'])
        if (RUN/'status.json').exists():print((RUN/'status.json').read_text())
        print('Report:',RUN/'index.html');return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,_,_=prepare(RUN)
            print(json.dumps({k:plan[k] for k in ('stage','total_cases','new_points','diagnostic_batches','forecast')},indent=2));return 0
        # Child-local adapters only; do not mutate historical modules on disk or pilot RUN.
        base.RUN=RUN;scan.launch_command=p.prior.display_launch
        return execute()


if __name__=='__main__':raise SystemExit(main())
