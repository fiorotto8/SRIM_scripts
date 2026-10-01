#!/usr/bin/env python3
"""C3a: fixed independent-batch bulk pilot; no reuse of surface or paired histories."""
from __future__ import annotations
import argparse
from collections import defaultdict
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
from scripts import review_bulk_corners as previous
from scripts.preliminary_report import atomic_text,csv_text,table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample,memory_info,GIB
prior=previous.prior
base=prior.base
scan=base.scan
RUN=ROOT/'runs/noble_cf4_bulk_precision_pilot'
UNIT='srim-bulk-precision-pilot.service'
MODEL='noble-cf4-bulk-trim5-eps1e9-v1'
BUDGET=6*3600
WORKERS=15
JOB_TIMEOUT=3*3600


def make_input(original,name,seed,z,energy):
    if energy not in (1.,100.,1000.) or z not in (6,9,18,36,54) or not 0<seed<900000001:
        raise ValueError('Unexpected pilot parameters')
    lines=original.decode('ascii').splitlines()
    ion=lines[next(i for i,x in enumerate(lines) if x.startswith('Ion:'))+1].split()
    if int(ion[0])!=z or float(ion[2])!=energy or int(ion[4])!=50:raise ValueError('Source primary mismatch')
    index=next(i for i,x in enumerate(lines) if x.startswith('1 "'))
    layer=shlex.split(lines[index]);origin=float(layer[2]);width=origin*2
    lines[index]=f'1 "{layer[1]}" {width:.17g} '+' '.join(layer[3:])
    index=next(i for i,x in enumerate(lines) if x.startswith('Cascades('))+1
    fields=lines[index].split();fields[:2]=['5',str(seed)];lines[index]=' '.join(fields)
    index=next(i for i,x in enumerate(lines) if x.startswith('Diskfiles'))+1
    fields=lines[index].split();fields[0]='1';lines[index]=' '.join(fields)
    index=next(i for i,x in enumerate(lines) if x.startswith('PlotType'))+1
    lines[index]=f'5 0.0 {width:.17g}'
    lines[lines.index('Target material : Number of Elements, Number of Layers')+1]=f'"{name}" 3 1'
    header=['TRIM.DAT independent bulk precision pilot','Ten comment lines; CRLF and DOS EOF',
            'Mode 5 full cascade; central origin in target of depth 2W','Exactly fifty primaries per independent batch',
            'Energy in eV, positions in Angstrom','Direction +X; Y=Z=0','Five-character event identity',
            f'{name} Z={z} E={energy:g}keV','Event Z Energy X Y Z CosX CosY CosZ','BEGIN EVENTS AFTER TENTH LINE']
    events=[f'E{i:04d} {z} {energy*1000:.17g} {origin:.17g} 0 0 1 0 0' for i in range(1,51)]
    return ('\r\n'.join(lines)+'\r\n').encode('ascii'),('\r\n'.join(header+events)+'\r\n').encode('ascii')+b'\x1a',origin,width


def prepare(run):
    review,diagnostics,_,_=previous.review()
    old=base.d.prior.previous.read_rows(base.d.prior.previous.RUN)+base.d.prior.fresh_rows(base.d.prior.RUN)
    records={}
    for r in sorted(old,key=lambda r:r['repeat']):
        if r['noble_percent']==80 and r['pressure_atm']==1 and r['energy_kev'] in (1,100,1000):
            records.setdefault((r['family'],r['ion'],r['energy_kev']),r)
    if len(records)!=27:raise ValueError('Expected 27 pilot source points')
    forbidden={r['random_seed'] for r in old}|{r['seed'] for r in diagnostics};used=set(forbidden)
    measured={(r['family'],r['ion'],r['energy_kev']):r['wall_seconds'] for r in diagnostics if r['geometry']=='interior'}
    reference=base.schedule.timing_means(base.schedule.TIMINGS)
    cases=[];inputs={}
    paths={Path(__file__),ROOT/'scripts/bulk_precision_pilot.sh',Path(previous.__file__),
           previous.RUN/'plan.json',previous.RUN/'completion.json',previous.RUN/'results.json',
           Path(prior.__file__),Path(prior.display.__file__),base.schedule.TIMINGS}
    for r in diagnostics:
        paths.add(previous.RUN/'cases'/r['name']/'result.json');paths.update(Path(p) for p in r['artifact_sha256'])
    for (family,ion,energy),row in sorted(records.items()):
        source=Path(row['raw_archive']);paths.add(source)
        for repeat in range(4):
            point=f'{family}_{ion}_e{energy:g}';name=f'{point}_b{repeat}'
            seed=scan.seed_for(MODEL+'-pilot-'+name,repeat)
            if seed in used:raise ValueError('Seed collision; never silently replace seed')
            used.add(seed)
            content,events,origin,width=make_input(base.d.archive_data(source,'TRIM.IN'),name,seed,row['ion_z'],energy)
            inputs[name]=(content,events)
            cases.append(dict(name=name,point=point,repeat=repeat,phase='low_energy' if energy==1 else 'remaining',
                              family=family,ion=ion,ion_z=row['ion_z'],energy_kev=energy,noble_percent=80,pressure_atm=1.,
                              geometry='interior',model_id=MODEL,ions=50,seed=seed,source=str(source),
                              origin_angstrom=origin,width_angstrom=width,expected=row['target_energy_check']['expected'],
                              input_sha256=hashlib.sha256(content).hexdigest(),trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                              estimated_seconds=measured.get((family,ion,energy),reference[family,ion,energy,80,1.])))
    phases={}
    for phase in ('low_energy','remaining'):
        loads=[0.]*WORKERS
        for cost in sorted((r['estimated_seconds'] for r in cases if r['phase']==phase),reverse=True):heapq.heapreplace(loads,loads[0]+cost)
        phases[phase]=max(loads)/3600
    forecast=dict(phase_model_hours=phases,model_hours=sum(phases.values()),planning_hours=1.5*sum(phases.values())+.5,
                  longest_reference_batch_hours=max(r['estimated_seconds'] for r in cases)/3600,
                  method='Measured interior endpoint-energy costs; old-model 100keV timings except directly measured Kr/F100keV. '
                         '15-worker longest-first scheduling; low-energy gate then remaining points; 50% margin plus 30 minutes. Not guaranteed.')
    if len(cases)!=108 or len(used-forbidden)!=108 or forecast['planning_hours']>4:raise ValueError('Pilot count/forecast changed')
    plan=dict(stage='C3a',model_id=MODEL,model_status='provisional_independent_batch_pilot_not_full_grid_release',cases=cases,
              total_cases=108,points=27,new_histories=5400,retained_histories=0,independent_batches_per_point=4,
              budget_hours=6,job_timeout_seconds=JOB_TIMEOUT,workers=WORKERS,forecast=forecast,previous_review=review,
              input_sha256={str(p):scan.sha256_file(p) for p in sorted(paths)},
              design='Ar/Kr/Xe with C/F/noble recoils at 1/100/1000keV, 80% noble, 1 atm, 293.15K. '
                     'Four fresh 50-ion batches per point; fixed 108 unique seeds. Full-cascade mode5, central starts W in 2W targets. '
                     'Displacement 21eV, lattice/surface binding 1e-9eV; preserve validated source physics and stopping bounds.',
              quantity='Mean signed projected displacement endpoint X minus initial X over all 50 contained primaries; '
                       'integrated primary+secondary ionization divided by initial energy. Not total track length or detector-calibrated quenching.',
              gates='Complete input hashes, actual ion/energy/bindings, all 50 endpoints, zero primary escape and zero edge-bin signal; '
                    'authenticated display readiness, exact normal-completion acknowledgement and normal process exit. '
                    'All 36 low-energy cases must pass integrity before remaining 72 cases. Precision is assessed only after four independent batches: '
                    'SEM/mean <=5% range and <=2% ionization. Precision shortfalls are reported, not silently resampled or treated as software errors.',
              limitations='This provisional bulk baseline excludes all surface results and paired diagnostic histories. '
                          'Single-seed depth pairs are not independent repeats. Estimated SEM is not model accuracy or a coverage guarantee. '
                          'No pressure equivalence, concentration-wide validity or automatic full-grid production release.',
              next='Review independence, precision, intermediate-energy containment and timing; then freeze the remaining 216-point '
                   'bulk-baseline rebuild or a bounded follow-up if evidence requires it. Existing 243 surface points stay separate.')
    path=run/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan:raise ValueError('Frozen independent bulk pilot changed')
    if not path.exists():write_json(path,plan)
    return plan,inputs


def point_summary(rows):
    groups=defaultdict(list);seeds=set()
    for r in rows:
        if r['status']!='COMPLETE':continue
        if r['model_id']!=MODEL or r['seed'] in seeds or r['ions_confirmed']!=50:raise ValueError('Mixed model, duplicate seed or wrong ion count')
        seeds.add(r['seed']);groups[r['point']].append(r)
    points=[]
    for key,rr in sorted(groups.items()):
        if len(rr)>4 or len({r['repeat'] for r in rr})!=len(rr):raise ValueError('Duplicate or excessive batches')
        if any(r['repeat'] not in range(4) for r in rr):raise ValueError('Unexpected repeat')
        x=rr[0];p={k:x[k] for k in ('point','family','ion','energy_kev','noble_percent','pressure_atm','model_id')}
        p.update(batches=len(rr),ions=50*len(rr),complete=len(rr)==4)
        for field,name in [('stopped_ion_mean_dx_mm','projected_range_mm'),('ionization_fraction','ionization_fraction')]:
            values=[r[field] for r in rr]
            if not all(math.isfinite(v) and v>0 for v in values):raise ValueError('Invalid batch observable')
            mean=statistics.mean(values);sem=statistics.stdev(values)/math.sqrt(len(rr)) if len(rr)>1 else None
            p.update({name:mean,name+'_sem':sem,name+'_relative_sem':sem/mean if sem is not None else None})
        p['precision_met']=p['complete'] and p['projected_range_mm_relative_sem']<=.05 and p['ionization_fraction_relative_sem']<=.02
        points.append(p)
    return points


def run_case(case,contents,runtime,context,deadline):
    (runtime/'.display-startup.json').unlink(missing_ok=True);(runtime/'.trim-process.log').unlink(missing_ok=True)
    result=prior.old_run_case(case,contents,runtime,context,deadline)
    metadata=runtime/'.display-startup.json'
    if metadata.exists():
        target=Path(result['attempt'])/metadata.name;shutil.copy2(metadata,target)
        result['artifact_sha256'][str(target)]=scan.sha256_file(target);result['display_startup']=json.loads(target.read_text())
    if result['status']=='COMPLETE' and result.get('display_startup',{}).get('allocation')!='Xvfb -displayfd':
        result.update(status='FAILED',error='Missing atomic display evidence')
    write_json(RUN/'cases'/case['name']/'result.json',result)
    return result


def publish(state,rows):
    points=point_summary(rows)
    state.update(points_present=len(points),points_complete=sum(p['complete'] for p in points),
                 points_meeting_precision=sum(p['precision_met'] for p in points),
                 completed_points_flagged=sum(p['complete'] and not p['precision_met'] for p in points))
    write_json(RUN/'status.json',state);write_json(RUN/'results.json',rows);write_json(RUN/'points.json',points)
    cols=['name','status','model_id','point','family','ion','energy_kev','noble_percent','pressure_atm','repeat','seed',
          'ionization_fraction','stopped_ion_mean_dx_mm','reported_range_summary_mm','wall_seconds','attempt']
    atomic_text(RUN/'batches.csv',csv_text(rows,cols))
    if points:atomic_text(RUN/'points.csv',csv_text(points,list(points[0])))
    body='''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>C3a independent bulk precision pilot</title><style>body{font:16px system-ui;margin:30px}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>C3a: independent-batch bulk pilot — full-grid production held</h1>
<p>27 points: Ar/Kr/Xe, C/F/noble recoils, 1/100/1000keV, 80% noble, 1 atm.
Four fresh 50-ion batches per point, 108 unique seeds. No surface or paired-diagnostic histories reused.
Projected range is mean endpoint X minus initial X over all contained primaries, not absolute RANGE.TXT or track length.
Ionization fraction integrates primary and secondary ionization; it is not detector-calibrated quenching.</p>
<p>Fixed samples: estimated SEM targets are 5% range and 2% ionization. Flags remain visible; no automatic top-ups.
Integrity failures stop submission. All 36 low-energy cases gate the remaining 72 cases.
15-core maximum, 24/32GiB RAM, zero scan swap, six-hour cumulative cap.</p>
<p><a href="points.csv">Points CSV</a> · <a href="batches.csv">Independent batches CSV</a> ·
<a href="plan.json">Frozen plan and forecast</a> · <a href="logs/campaign.log">Log</a> ·
<a href="../noble_cf4_bulk_corners_retry/review/index.html">Audited preceding checks</a></p>'''
    body+=table([state],list(state))
    if points:body+=table(points,list(points[0]))
    if (RUN/'pilot.pdf').exists():body+='<p><a href="pilot.pdf">PDF plots</a></p><img src="pilot.png" style="max-width:100%">'
    atomic_text(RUN/'index.html',body)


def plots(rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    points=point_summary(rows);fig,axes=plt.subplots(3,2,figsize=(11,12),squeeze=False)
    for row,family in enumerate(('Ar','Kr','Xe')):
        for ion in ('C','F',family):
            group=sorted((p for p in points if p['family']==family and p['ion']==ion),key=lambda p:p['energy_kev'])
            for col,metric in enumerate(('projected_range_mm','ionization_fraction')):
                ax=axes[row,col];ax.errorbar([p['energy_kev'] for p in group],[p[metric] for p in group],
                                           yerr=[p[metric+'_sem'] for p in group],marker='o',capsize=3,label=ion)
                ax.set_xscale('log');ax.set_xlabel('Recoil energy [keV]');ax.grid(True,alpha=.3);ax.legend()
                ax.set_title(f'{family}80/CF4-20, 1 atm, 200 ions/point')
                ax.set_ylabel('Projected displacement [mm]' if col==0 else 'SRIM ionization fraction')
                if col==0:ax.set_yscale('log')
    fig.suptitle('Independent bulk pilot — error bars: batch SEM; not detector-calibrated quenching')
    fig.tight_layout();fig.savefig(RUN/'pilot.pdf');fig.savefig(RUN/'pilot.png',dpi=130);plt.close(fig)


def execute():
    group=Path(resource_sample()['cgroup'])
    if (group.name!=UNIT or len(os.sched_getaffinity(0))>WORKERS or
        (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0'):
        raise RuntimeError('Use bulk_precision_pilot.sh for enforced resources')
    used=json.loads((RUN/'budget.json').read_text())['used_seconds'] if (RUN/'budget.json').exists() else 0.
    if used>=BUDGET or memory_info()['MemAvailable']<16*GIB:raise RuntimeError('Insufficient time/memory budget')
    if (RUN/'completion.json').exists():raise RuntimeError('Already complete; review instead of relaunching')
    started=time.monotonic();deadline=started+BUDGET-used;rows=[]
    state=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),phase='Preparing independent pilot',model_id=MODEL)
    def stop(sig,frame):scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop)
    def update():
        elapsed=used+time.monotonic()-started
        if elapsed>=BUDGET:scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(),completed_cases=sum(r['status']=='COMPLETE' for r in rows),
                     expected_cases=108,cumulative_hours=elapsed/3600,resources=resource_sample())
        write_json(RUN/'budget.json',dict(used_seconds=elapsed,limit_seconds=BUDGET));publish(state,rows)
    try:
        update();plan,inputs=prepare(RUN)
        cfg=scan.load_config(base.d.CONFIG);context=scan.resolve_execution_context(cfg)
        runtimes,_=scan.prepare_runtimes(scan.resolve_srim_root(None,cfg),RUN,WORKERS,context)
        write_json(RUN/f'session-{os.getpid()}.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                                                        cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase in ('low_energy','remaining'):
            state['phase']=phase;pending=[]
            for case in plan['cases']:
                if case['phase']!=phase:continue
                result=base.cached(case)
                if result:rows.append(result)
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
                        try:result=future.result()
                        except Exception:scan.SHUTDOWN_REQUESTED.set();raise
                        rows.append(result);print(json.dumps({k:v for k,v in result.items() if k!='artifact_sha256'}),flush=True)
                        if result['status']!='COMPLETE':scan.SHUTDOWN_REQUESTED.set()
                    update()
            if any(r['status']=='FAILED' for r in rows):raise RuntimeError('Integrity failure; remaining cases held')
            if scan.SHUTDOWN_REQUESTED.is_set():raise InterruptedError('Stopped or insufficient cumulative budget')
            if phase=='low_energy' and (len(rows)!=36 or not all(p['complete'] for p in point_summary(rows))):
                raise RuntimeError('All low-energy integrity canaries required')
            update()
        prepare(RUN);points=point_summary(rows)
        if len(rows)!=108 or len(points)!=27 or not all(p['complete'] for p in points):raise ValueError('Incomplete pilot')
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill',0)):
            raise RuntimeError('OOM observed')
        plots(rows)
        state.update(status='REVIEW_REQUIRED',phase='Independent bulk pilot complete; review precision and baseline gate',source_unchanged=True)
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
            plan,_=prepare(RUN);print(json.dumps({k:plan[k] for k in ('stage','model_id','points','total_cases','new_histories','forecast')},indent=2));return 0
        # Only reuse the generic case executor/cache and authenticated display adapter.
        # Keep prior source files and completed-run globals outside this child unchanged.
        base.RUN=RUN;scan.launch_command=prior.display_launch
        return execute()


if __name__=='__main__':raise SystemExit(main())
