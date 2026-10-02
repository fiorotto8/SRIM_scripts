#!/usr/bin/env python3
"""P2a: eighteen fixed 1MeV pressure timing and containment canaries."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from datetime import datetime
from decimal import Decimal
import fcntl
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import review_bulk_baseline as previous
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample,memory_info,GIB
r,b,scan=previous.prior,previous.b,previous.scan
RUN=ROOT/'runs/noble_cf4_high_energy_pressure'
UNIT='srim-high-energy-pressure.service'
MODEL=b.MODEL
WORKERS=15
BUDGET=6*3600
JOB_TIMEOUT=3*3600
PRESSURE_SPEC=dict(comparisons=36,familywise_alpha=.05,ionization_ratio_margin=[.95,1.05],
                   pressure_scaled_range_ratio_margin=[.90,1.10],batches_per_condition_after_p2b=4,
                   method='After P2b only: approximate Welch intervals for differences of mean log batch observables, '
                   'Bonferroni 36 two-sided comparisons (9 family/recoil combinations x 2 pressures x 2 metrics) '
                   'at 1000keV,80% noble. Pressure-scaled displacement and ionization fraction. Exponentiated '
                   'intervals are geometric batch-mean ratios. New pressure samples have four independent '
                   '50-ion batches per point including P2a b0; reference is the existing audited four-batch '
                   '1atm baseline. Inside working margin, entirely outside or inconclusive are all reported. '
                   'Zero estimated variance is unresolved; approximate n=4 log-batch coverage, not exact. '
                   'Reference observations were visible when freezing this screen; any selected effect needs a separate holdout.')


def layer_fields(content):
    return shlex.split(next(x for x in content.decode('ascii').splitlines() if x.startswith('1 "')))


def make_input(original,pressure_source,name,seed,z,pressure):
    if pressure not in (.5,3.):raise ValueError('Unexpected high-energy pressure')
    layer=layer_fields(original);control=layer_fields(pressure_source)
    density=float(control[3]);reference=float(layer[3]);width=float(layer[2])*reference/density
    if not math.isclose(density/reference,pressure,rel_tol=1e-12):raise ValueError('Input density does not represent requested pressure')
    if len(layer)!=len(control) or any(not math.isclose(float(a),float(c),rel_tol=1e-12,abs_tol=1e-15)
                                    for a,c in zip(layer[4:],control[4:])):raise ValueError('Pressure-source stoichiometry changed')
    lines=original.decode('ascii').splitlines();index=next(i for i,x in enumerate(lines) if x.startswith('1 "'))
    lines[index]=f'1 "{control[1]}" {width:.17g} {density:.17g} '+' '.join(layer[4:])
    adjusted=('\r\n'.join(lines)+'\r\n').encode('ascii')
    content,events,origin,target=b.make_input(adjusted,name,seed,z,1000.,2)
    return content,events,origin,target,density


def prepare(run):
    review,rows,_,timing,_=previous.review()
    records={}
    for row in sorted(rows,key=lambda x:x['repeat']):records.setdefault(b.key(row),row)
    times={b.key(x):x['mean_seconds'] for x in timing}
    legacy=b.base.d.prior.previous.read_rows(b.base.d.prior.previous.RUN)+b.base.d.prior.fresh_rows(b.base.d.prior.RUN)
    forbidden={x['seed'] for x in rows}|{x['random_seed'] for x in legacy}
    for path in (b.p.previous.RUN/'results.json',r.OLD_RUN/'results.json'):
        forbidden.update(x['seed'] for x in json.loads(path.read_text()))
    cases=[];inputs={};reserved={};used=set(forbidden)
    paths={Path(__file__),ROOT/'scripts/high_energy_pressure.sh',Path(previous.__file__),Path(r.worker.__file__),
           previous.RUN/'plan.json',previous.RUN/'completion.json',previous.RUN/'results.json',previous.RUN/'points.json'}
    for row in rows:
        paths.update(Path(p) for p in row['artifact_sha256'])
    for family in ('Ar','Kr','Xe'):
        for ion in ('C','F',family):
            source=records[family,ion,1000.,80,1.];archive=Path(source['source']);paths.add(archive)
            original=b.base.d.archive_data(archive,'TRIM.IN')
            for pressure in (.5,3.):
                condition=records[family,ion,100.,80,pressure];pressure_archive=Path(condition['source']);paths.add(pressure_archive)
                control=b.base.d.archive_data(pressure_archive,'TRIM.IN')
                point=b.name_for((family,ion,1000.,80,pressure));name=point+'_b0';seeds=[]
                for repeat in range(4):
                    seed=scan.seed_for(MODEL+'-high-pressure-'+point,repeat)
                    if seed in used:raise ValueError('High-pressure independent seed collision')
                    used.add(seed);seeds.append(seed)
                reserved[point]=seeds
                content,events,origin,width,density=make_input(original,control,name,seeds[0],source['ion_z'],pressure)
                inputs[name]=(content,events)
                ratio=max(1.,*(times[family,ion,e,80,pressure]/times[family,ion,e,80,1.] for e in (1.,10.,100.)))
                cost=times[family,ion,1000.,80,1.]*ratio
                cases.append(dict(name=name,point=point,repeat=0,phase='high_pressure',diagnostic=False,model_id=MODEL,
                                  family=family,ion=ion,ion_z=source['ion_z'],energy_kev=1000.,noble_percent=80,
                                  pressure_atm=pressure,geometry='interior',ions=50,seed=seeds[0],source=str(archive),
                                  pressure_source=str(pressure_archive),density_g_cm3=density,
                                  origin_angstrom=origin,width_angstrom=width,expected=source['expected'],
                                  input_sha256=hashlib.sha256(content).hexdigest(),trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                                  estimated_seconds=cost))
    loads=[0.]*WORKERS
    for cost in sorted((c['estimated_seconds'] for c in cases),reverse=True):heapq.heapreplace(loads,loads[0]+cost)
    model=max(loads)/3600
    forecast=dict(model_hours=model,planning_hours=1.5*model+.5,longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases)/3600,
                  method='Measured 1atm 1MeV bulk mean times multiplied by maximum observed same-recoil pressure ratio '
                  'over1/10/100keV (never below one). 15-worker longest-first scheduling,50% margin plus30min. '
                  'Pressure cost extrapolation is replaced by measured P2a timings before P2b.')
    if len(cases)!=18 or len(used-forbidden)!=72 or forecast['planning_hours']>5 or forecast['longest_estimated_batch_hours']>2.5:
        raise ValueError('P2a count or budget gate failed')
    plan=dict(stage='P2a',model_id=MODEL,cases=cases,total_cases=18,new_points=18,histories=900,workers=WORKERS,
              budget_hours=6,job_timeout_seconds=JOB_TIMEOUT,forecast=forecast,previous_review=review,
              reserved_independent_seeds=reserved,pressure_screen_after_p2b=PRESSURE_SPEC,
              input_sha256={str(p):scan.sha256_file(p) for p in sorted(paths)},
              design='All Ar/Kr/Xe mixtures at80% noble, C/F/noble recoils,1000keV,0.5/3atm,293.15K. '
              'One fresh50-ion batch perpoint for timing/containment; four batch seeds reserved for P2a/P2b. '
              'Pressure density and composition come from audited100keV pressure inputs. Surface-reference '
              '1MeV width scaled inversely with actual density,then unchanged central bulk geometry W in2W.',
              gates='Same complete input/binding/species/energy/count/endpoint/no-escape/no-edge-signal gates. '
              'Actual printed mass density must agree within one unit of its last printed decimal (SRIM header truncation); '
              'executed input density is exact and checked against the audited pressure input. Atomic-density header '
              'is retained with its known formatting limitation. Normal completion,private display and helper evidence required.',
              next='Review actual heavy-recoil timings and containment,then freeze P2b three more batches perpoint. '
              'P2a has one batch/point and provides no independent-batch SEM; no automatic P2b or precision top-up.')
    path=run/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan:raise ValueError('Frozen P2a plan changed')
    if not path.exists():write_json(path,plan)
    return plan,inputs


def check_density(case,outputs):
    data=scan.find_output(outputs,'TDATA.TXT').read_text(encoding='latin1')
    match=re.search(r'Density\s*=.*?=\s*([\d.Ee+\-]+)\s*g/cm3',data)
    if match is None:raise ValueError('Actual printed mass density mismatch')
    reported=float(match[1]);quantum=float(Decimal(10)**Decimal(match[1]).as_tuple().exponent)
    if reported<=0 or quantum>1e-4 or abs(reported-case['density_g_cm3'])>quantum+1e-12:
        raise ValueError('Actual printed mass density mismatch')
    return dict(reported_density_g_cm3=reported,reported_density_resolution_g_cm3=quantum,density_parameters_match=True)


def audit_case(case,row,contents):
    b.previous.audit_case(case,row,contents)
    directory=Path(row['attempt']);fresh=check_density(case,{p.name.upper():p for p in directory.iterdir()})
    if any(row[k]!=v for k,v in fresh.items()):raise ValueError('Density analysis changed')
    probes=json.loads((directory/'.completion-probes.json').read_text())
    if probes!=row['completion_monitor'] or probes['helper']!=str(Path(r.worker.__file__).resolve()):raise ValueError('Monitor evidence mismatch')


def run_case(case,contents,runtime,context,deadline):
    (runtime/'.completion-probes.json').unlink(missing_ok=True)
    row=b.run_case(case,contents,runtime,context,deadline)
    metadata=runtime/'.completion-probes.json'
    if metadata.exists():
        target=Path(row['attempt'])/metadata.name;shutil.copy2(metadata,target)
        row['artifact_sha256'][str(target)]=scan.sha256_file(target);row['completion_monitor']=json.loads(target.read_text())
    if row['status']=='COMPLETE':
        try:
            row.update(check_density(case,scan.collect_runtime_outputs(runtime)))
            if 'completion_monitor' not in row:raise ValueError('Missing completion helper evidence')
        except Exception as exc:row.update(status='FAILED',error=str(exc))
    write_json(RUN/'cases'/case['name']/'result.json',row)
    return row


def publish(state,rows):
    state.update(completed_cases=sum(r['status']=='COMPLETE' for r in rows),expected_cases=18)
    write_json(RUN/'status.json',state);write_json(RUN/'results.json',rows)
    columns=['name','status','family','ion','energy_kev','pressure_atm','seed','ions_confirmed','stopped_ion_mean_dx_mm',
             'ionization_fraction','density_g_cm3','reported_density_g_cm3','wall_seconds','attempt']
    atomic_text(RUN/'batches.csv',csv_text(rows,columns))
    body='<html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>High-energy pressure canaries</title>'
    body+='<h1>P2a: 18 direct1MeV pressure points</h1><p>Ar/Kr/Xe80/CF4-20, C/F/noble recoils,0.5/3atm. '
    body+='One independent50-ion batch perpoint: timing and containment gate. Batch SEM awaits P2b. '
    body+='Range is projected endpoint minusstart; ionizationfraction requires detectorcalibration for quenching.</p>'
    body+='<p>15-core maximum,24/32GiB RAM,zero scan swap,6-hourcap. '
    body+='<a href="batches.csv">CSV</a> · <a href="plan.json">Frozen plan and subsequent pressure-screen criteria</a> · '
    body+='<a href="../noble_cf4_bulk_baseline_retry/review/index.html">Audited243-pointbaseline and plots</a></p>'
    body+=table([state],list(state))
    if rows:body+=table(rows,columns)
    atomic_text(RUN/'index.html',body)


def execute():
    group=Path(resource_sample()['cgroup'])
    if group.name!=UNIT or len(os.sched_getaffinity(0))>WORKERS or (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0':
        raise RuntimeError('Use resource-limited P2a launcher')
    used=json.loads((RUN/'budget.json').read_text())['used_seconds'] if (RUN/'budget.json').exists() else 0.
    if used>=BUDGET or memory_info()['MemAvailable']<16*GIB:raise RuntimeError('Insufficient time/RAM allowance')
    if (RUN/'completion.json').exists():raise RuntimeError('Already complete; review instead of relaunching')
    started=time.monotonic();deadline=started+BUDGET-used;rows=[]
    state=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),phase='Preparing high-energy pressure gate',model_id=MODEL)
    def stop(sig,frame):scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    def update():
        elapsed=used+time.monotonic()-started
        if elapsed>=BUDGET:scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(),cumulative_hours=elapsed/3600,resources=resource_sample())
        write_json(RUN/'budget.json',dict(used_seconds=elapsed,limit_seconds=BUDGET));publish(state,rows)
    try:
        update();plan,inputs=prepare(RUN)
        config=scan.load_config(b.base.d.CONFIG);context=scan.resolve_execution_context(config)
        runtimes,_=scan.prepare_runtimes(scan.resolve_srim_root(None,config),RUN,WORKERS,context)
        write_json(RUN/f'session-{os.getpid()}.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        pending=[]
        for case in plan['cases']:
            cached=b.base.cached(case)
            if cached:audit_case(case,cached,inputs[case['name']]);rows.append(cached)
            else:pending.append(case)
        pending.sort(key=lambda c:c['estimated_seconds'],reverse=True);state['phase']='high_energy_pressure'
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
                    try:row=future.result()
                    except Exception:scan.SHUTDOWN_REQUESTED.set();raise
                    rows.append(row);print(json.dumps({k:v for k,v in row.items() if k!='artifact_sha256'}),flush=True)
                    if row['status']!='COMPLETE':scan.SHUTDOWN_REQUESTED.set()
                update()
        if any(r['status']=='FAILED' for r in rows):raise RuntimeError('Integrity failure; remaining P2 work held')
        if scan.SHUTDOWN_REQUESTED.is_set():raise InterruptedError('Stopped or insufficient cumulative budget')
        prepare(RUN);indexed={row['name']:row for row in rows}
        if len(rows)!=18 or len(indexed)!=18:raise ValueError('Incomplete high-pressure gate')
        for case in plan['cases']:audit_case(case,indexed[case['name']],inputs[case['name']])
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill',0)):raise RuntimeError('OOM observed')
        state.update(status='REVIEW_REQUIRED',phase='P2a complete; review timing and containment before P2b',source_unchanged=True,
                     longest_batch_seconds=max(row['wall_seconds'] for row in rows))
        update();write_json(RUN/'completion.json',state);return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc,InterruptedError) else 'NEEDS_ATTENTION',error=str(exc))
        update();print(str(exc),file=sys.stderr,flush=True);return 1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('prepare','run','status'));args=parser.parse_args()
    if args.command=='status':
        subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'])
        if (RUN/'status.json').exists():print((RUN/'status.json').read_text())
        print('Report:',RUN/'index.html');return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,_=prepare(RUN);print(json.dumps({k:plan[k] for k in ('stage','total_cases','histories','forecast')},indent=2));return 0
        b.RUN=RUN;b.base.RUN=RUN;scan.launch_command=r.launch
        return execute()


if __name__=='__main__':raise SystemExit(main())
