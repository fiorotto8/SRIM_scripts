#!/usr/bin/env python3
"""Continue the frozen C3b baseline after its completion-window race, retaining audits."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import copy
from datetime import datetime
import fcntl
import heapq
import json
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bulk_baseline_rebuild as b
from scripts import trim_dat_completion_retry as worker
from scripts import completion_retry_canary as canary
from scripts.preliminary_report import atomic_text
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample,memory_info,GIB
scan=b.scan
OLD_RUN=b.RUN
RUN=ROOT/'runs/noble_cf4_bulk_baseline_retry'
UNIT='srim-bulk-baseline-retry.service'
FAILED_NAME='Xe_F_e1000_n95_p1_b0'
BUDGET=b.BUDGET
WORKERS=b.WORKERS
original_prepare=b.prepare


def review_failed():
    plan,inputs,pilot=original_prepare(OLD_RUN)
    state=json.loads((OLD_RUN/'status.json').read_text())
    rows=json.loads((OLD_RUN/'results.json').read_text());cases={c['name']:c for c in plan['cases']}
    if state['status']!='NEEDS_ATTENTION' or len(rows)!=147 or len({r['name'] for r in rows})!=147:
        raise ValueError('Unexpected failed baseline state/count')
    accepted=[]
    for row in rows:
        if row!=json.loads((OLD_RUN/'cases'/row['name']/'result.json').read_text()):raise ValueError('Result index changed')
        if any(row[k]!=v for k,v in cases[row['name']].items()):raise ValueError('Original case identity changed')
        for path,digest in row['artifact_sha256'].items():
            if scan.sha256_file(Path(path))!=digest:raise ValueError('Original artifacts changed')
        directory=Path(row['attempt'])
        if (directory/'TRIM.IN').read_bytes()!=inputs[row['name']][0] or (directory/'TRIM.DAT').read_bytes()!=inputs[row['name']][1]:
            raise ValueError('Original executed input changed')
        if row['status']=='COMPLETE':
            b.previous.audit_case(cases[row['name']],row,inputs[row['name']]);accepted.append(row)
    failed=[r for r in rows if r['status']=='FAILED']
    if len(failed)!=1 or failed[0]['name']!=FAILED_NAME or 'Bad Window' not in failed[0]['error'] or 'Bad Drawable' not in failed[0]['error']:
        raise ValueError('Unexpected failure cause; do not apply window-race recovery')
    directory=Path(failed[0]['attempt']);ack=json.loads((directory/'completion-ack.json').read_text())
    if ack['status']!='sent' or ack['title']!=b.base.completion.TITLE or (directory/'IONIZ.TXT').exists():
        raise ValueError('Failure lifecycle differs from diagnosis')
    checks=b.geometry_checks(accepted)
    if len(accepted)!=132 or sum(r['diagnostic'] for r in accepted)!=42 or len(checks)!=42 or not all(c['passed'] for c in checks):
        raise ValueError('Retained baseline or geometry gate incomplete')
    budget=json.loads((OLD_RUN/'budget.json').read_text())
    summary=dict(status='AUDITED_COMPLETION_WINDOW_RACE',complete_cases=132,independent_new_batches=90,
                 depth_diagnostic_batches=42,depth_pairs_passed=42,failed_cases=1,stopped_cases=14,
                 unlaunched_cases=759,remaining_cases=774,inherited_used_seconds=budget['used_seconds'],resources=state['resources'],
                 cause='After the exact completion dialog was acknowledged, a redundant xwininfo tree query '
                 'hit a disappearing Wine window (BadWindow/BadDrawable). The monitor raised and cleanup '
                 'interrupted final output. Partial data remain rejected.',
                 decision='Retain 132 raw-audited cases. Run the failed case first with the tested completion '
                 'monitor; require full output/exit evidence before releasing 773 remaining original cases. '
                 'Same inputs, seeds, model and pressure/precision gates. Carry prior and software-canary time into the original 12-hour cap.')
    return plan,inputs,pilot,rows,accepted,summary


def recovery_forecast(cases,accepted):
    grouped={}
    for row in accepted:grouped.setdefault(b.key(row),[]).append(row['wall_seconds'])
    means={k:statistics.mean(v) for k,v in grouped.items()}
    names={r['name'] for r in accepted};pending=[c for c in cases if c['name'] not in names]
    costs={c['name']:max(c['estimated_seconds'],means.get(b.key(c),0)) for c in pending}
    canary_seconds=costs[FAILED_NAME];loads=[0.]*WORKERS
    for cost in sorted((v for name,v in costs.items() if name!=FAILED_NAME),reverse=True):heapq.heapreplace(loads,loads[0]+cost)
    model=(canary_seconds+max(loads))/3600
    return dict(remaining_cases=len(pending),canary_seconds=canary_seconds,model_hours=model,
                planning_hours=1.5*model+.5,longest_batch_seconds=max(costs.values()),
                method='First failed 50-ion input runs alone; then 15-worker longest-first continuation. '
                'Costs are max(frozen estimate, successful measured mean at the same condition). '
                '50% margin plus 30 minutes. No resetting spent time.'),costs


def validate_canary_primary(data):
    ion=re.search(r'Ion\s*=\s*(\w+)\s*\(\s*(\d+)\)',data)
    energy=re.search(r'Energy\s*=\s*([\d.Ee+\-]+)\s*keV',data)
    if ion is None or ion[1]!='F' or int(ion[2])!=9 or energy is None or float(energy[1])!=1000.:
        raise ValueError('Software canary primary mismatch')


def review_software_canary():
    test=json.loads((canary.RUN/'result.json').read_text())
    test_plan,content,events,source=canary.prepare()
    if not test['source_unchanged'] or (test['status']!='COMPLETE' and
        not (test['status']=='NEEDS_ATTENTION' and test.get('error')=='Canary primary mismatch')):
        raise ValueError('Unexpected software canary failure')
    for path,digest in {**test_plan['code_sha256'],**test['artifact_sha256']}.items():
        if scan.sha256_file(Path(path))!=digest:raise ValueError('Software canary provenance changed')
    directory=canary.RUN/'artifacts';outputs={p.name.upper():p for p in directory.iterdir()}
    for name in (*scan.STANDARD_TRIM_OUTPUTS,'RANGE_3D.TXT','BACKSCAT.TXT','TRANSMIT.TXT'):scan.find_output(outputs,name)
    if scan.find_output(outputs,'TRIM.IN').read_bytes()!=content or scan.find_output(outputs,'TRIM.DAT').read_bytes()!=events:
        raise ValueError('Software canary input changed')
    data=scan.find_output(outputs,'TDATA.TXT').read_text(encoding='latin1');validate_canary_primary(data)
    count=re.search(r'Total Ions calculated\s*=\s*([\d.]+)',data)
    if count is None or float(count[1])!=1 or not scan.check_target_energies(data,source['expected'])['passed']:
        raise ValueError('Software canary count or bindings mismatch')
    back=scan.count_escape_records(scan.find_output(outputs,'BACKSCAT.TXT'),'B')
    transmitted=scan.count_escape_records(scan.find_output(outputs,'TRANSMIT.TXT'),'T')
    endpoint=b.base.bulk.endpoints(scan.find_output(outputs,'RANGE_3D.TXT').read_text(encoding='latin1'),
                                 1,0,source['width_angstrom'],source['origin_angstrom'])
    if back or transmitted:raise ValueError('Software canary primary escaped')
    q=scan.parse_ioniz(scan.find_output(outputs,'IONIZ.TXT'),1000.)
    if not 0<q.qf_ioniz<=1:raise ValueError('Software canary ionization invalid')
    ack=json.loads((directory/'completion-ack.json').read_text());progress=json.loads((directory/'.trim-progress.json').read_text())
    display=json.loads((directory/'.display-startup.json').read_text());probes=json.loads((directory/'.completion-probes.json').read_text())
    if ack['status']!='sent' or ack['title']!=worker.original.TITLE or progress['status']!='complete':
        raise ValueError('Software canary completion evidence missing')
    if not display['authenticated'] or display['tcp_listening'] or display['allocation']!='Xvfb -displayfd' or probes['helper']!=str(Path(worker.__file__).resolve()):
        raise ValueError('Software canary monitor/display evidence mismatch')
    summary=dict(status='COMPLETE',raw_status=test['status'],ions_confirmed=1,wall_seconds=test['wall_seconds'],
                 source_unchanged=True,ionization_fraction=q.qf_ioniz,resources=test['resources'],**endpoint,
                 note='Normal one-ion completion raw-audited. Numeric energy parser accepts SRIM 1000000.E-03 keV. '
                 'Original canary result and frozen literal-format validator are preserved; no new history required.')
    write_json(canary.RUN/'review.json',summary)
    return dict(test,**{k:v for k,v in summary.items() if k not in ('note','raw_status')}),test_plan,summary


def prepare(run):
    old,inputs,pilot,records,accepted,review=review_failed()
    test,test_plan,software_review=review_software_canary()
    forecast,costs=recovery_forecast(old['cases'],accepted)
    inherited=review['inherited_used_seconds']+test['wall_seconds']
    if inherited+forecast['planning_hours']*3600>BUDGET:raise ValueError('Recovery forecast exceeds remaining original budget')
    paths={Path(__file__),ROOT/'scripts/bulk_baseline_retry.sh',Path(worker.__file__),Path(canary.__file__),
           OLD_RUN/'plan.json',OLD_RUN/'status.json',OLD_RUN/'results.json',OLD_RUN/'budget.json',
           canary.RUN/'plan.json',canary.RUN/'result.json'}
    for row in records:
        paths.add(OLD_RUN/'cases'/row['name']/'result.json');paths.update(Path(p) for p in row['artifact_sha256'])
    paths.update(Path(p) for p in test['artifact_sha256'])
    plan=copy.deepcopy(old);plan['input_sha256'].update({str(p):scan.sha256_file(p) for p in sorted(paths)})
    plan.update(stage='C3bR',retained_cases=[r['name'] for r in accepted],new_cases_to_run=774,
                original_failed_run=str(OLD_RUN),failure_review=review,inherited_used_seconds=inherited,
                software_canary_seconds=test['wall_seconds'],software_canary_review=software_review,forecast=forecast,recovery_costs=costs,
                recovery_gate='Exact original failed case first, requiring normal exit, all 50 endpoints, '
                'complete outputs and bindings. Only then run other 773 original cases. No extra seeds or altered inputs.',
                monitor_change='Original error watchdog runs first. After one acknowledgement, its existing 20-second '
                'exit grace avoids redundant completion-tree probes. Pre-acknowledgement BadWindow/BadDrawable '
                'inspection retries are bounded at three; persistent/unknown failures stop the run.')
    target=run/'plan.json'
    if target.exists() and json.loads(target.read_text())!=plan:raise ValueError('Frozen recovery plan changed')
    if not target.exists():write_json(target,plan)
    for row in accepted:
        target=run/'cases'/row['name']/'result.json';value=dict(row,reused_from=str(OLD_RUN/'cases'/row['name']/'result.json'))
        if target.exists() and json.loads(target.read_text())!=value:raise ValueError('Retained result changed')
        if not target.exists():write_json(target,value)
    path=run/'budget.json'
    if not path.exists():write_json(path,dict(used_seconds=inherited,limit_seconds=BUDGET))
    elif json.loads(path.read_text())['used_seconds']<inherited:raise ValueError('Inherited budget reset')
    write_json(run/'previous_review.json',review)
    return plan,inputs,pilot


def launch(command,cwd,timeout_s,*args,**kwargs):
    expected=['xvfb-run','-a','-e',str(cwd/'.xvfb.log'),'-s','-screen 0 1024x768x24']
    if list(command[:6])!=expected or list(command[6:9])!=[sys.executable,str(Path(b.base.__file__).resolve()),'worker']:
        raise ValueError('Unexpected worker command; no implicit rewrite')
    rewritten=[sys.executable,str(b.p.prior.display.__file__),'--runtime',str(cwd),'--',
               sys.executable,str(worker.__file__),*command[9:]]
    return b.p.prior.old_launch(rewritten,cwd,timeout_s,*args,**kwargs)


def run_case(case,contents,runtime,context,deadline):
    (runtime/'.completion-probes.json').unlink(missing_ok=True)
    row=b.run_case(case,contents,runtime,context,deadline)
    metadata=runtime/'.completion-probes.json'
    if metadata.exists():
        target=Path(row['attempt'])/metadata.name;shutil.copy2(metadata,target)
        row['artifact_sha256'][str(target)]=scan.sha256_file(target);row['completion_monitor']=json.loads(target.read_text())
    if row['status']=='COMPLETE' and 'completion_monitor' not in row:row.update(status='FAILED',error='Missing completion-monitor evidence')
    write_json(RUN/'cases'/case['name']/'result.json',row)
    return row


def publish(state,rows,pilot):
    state.update(retained_original_cases=132,new_completed_cases=sum(r['status']=='COMPLETE' and 'reused_from' not in r for r in rows))
    b.publish(state,rows,pilot)
    path=RUN/'index.html';body=path.read_text().replace('C3b: compatible bulk baseline','C3bR: completion-monitor recovery')
    body=body.replace('<h1>','<p>132 audited original cases retained. Failed case replay gates the other 773 original cases. '
                      'Original 12-hour cumulative budget includes prior run and software canary.</p><h1>',1)
    atomic_text(path,body)


def execute():
    group=Path(resource_sample()['cgroup'])
    if group.name!=UNIT or len(os.sched_getaffinity(0))>WORKERS or (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0':
        raise RuntimeError('Use resource-limited recovery launcher')
    plan,inputs,pilot=prepare(RUN);used=json.loads((RUN/'budget.json').read_text())['used_seconds']
    if used>=BUDGET or memory_info()['MemAvailable']<16*GIB:raise RuntimeError('Insufficient budget or RAM')
    if (RUN/'completion.json').exists():raise RuntimeError('Already complete; review instead of relaunching')
    start=time.monotonic();deadline=start+BUDGET-used;rows=[];pending=[]
    for case in plan['cases']:
        cached=b.base.cached(case)
        if cached:
            b.previous.audit_case(case,cached,inputs[case['name']]);rows.append(cached)
        else:pending.append(case)
    state=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),phase='Replay failed completion case',model_id=b.MODEL,
               inherited_hours=plan['inherited_used_seconds']/3600)
    def stop(sig,frame):scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stop)
    def update():
        elapsed=used+time.monotonic()-start
        if elapsed>=BUDGET:scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(),cumulative_hours=elapsed/3600,resources=resource_sample())
        write_json(RUN/'budget.json',dict(used_seconds=elapsed,limit_seconds=BUDGET));publish(state,rows,pilot)
    try:
        update();config=scan.load_config(b.base.d.CONFIG);context=scan.resolve_execution_context(config)
        runtimes,_=scan.prepare_runtimes(scan.resolve_srim_root(None,config),RUN,WORKERS,context)
        write_json(RUN/f'session-{os.getpid()}.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase in ('software_replay_gate','remaining_baseline'):
            state['phase']=phase
            work=[c for c in pending if (c['name']==FAILED_NAME)==(phase=='software_replay_gate')]
            work.sort(key=lambda c:plan['recovery_costs'][c['name']],reverse=True)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                available,active=list(runtimes),{}
                while work or active:
                    while work and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline-time.monotonic()<b.JOB_TIMEOUT+90:scan.SHUTDOWN_REQUESTED.set();break
                        case,runtime=work.pop(0),available.pop()
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
            if any(r['status']=='FAILED' for r in rows):raise RuntimeError('Case failure; dependent baseline held')
            if scan.SHUTDOWN_REQUESTED.is_set():raise InterruptedError('Stopped or insufficient original cumulative budget')
            if phase=='software_replay_gate':
                gate=next((r for r in rows if r['name']==FAILED_NAME),None)
                if gate is None or gate['status']!='COMPLETE' or 'completion_monitor' not in gate:raise ValueError('Replay gate incomplete')
                b.previous.audit_case(next(c for c in plan['cases'] if c['name']==FAILED_NAME),gate,inputs[FAILED_NAME])
            update()
        prepare(RUN)
        indexed={r['name']:r for r in rows}
        if len(rows)!=906 or len(indexed)!=906:raise ValueError('Incomplete case accounting')
        for case in plan['cases']:b.previous.audit_case(case,indexed[case['name']],inputs[case['name']])
        checks=b.geometry_checks(rows)
        if len(checks)!=42 or not all(c['passed'] for c in checks):raise ValueError('Geometry gate failed')
        combined=b.production_rows(pilot,rows);points=b.p.point_summary(combined)
        if len(points)!=243 or not all(p['complete'] for p in points):raise ValueError('Incomplete baseline points')
        pressure=b.pressure_checks(combined)
        if len(pressure)!=108:raise ValueError('Incomplete pressure family')
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill',0)):raise RuntimeError('OOM observed')
        write_json(RUN/'pressure_checks.json',pressure)
        atomic_text(RUN/'pressure_checks.csv',b.csv_text(pressure,sorted({k for row in pressure for k in row})))
        state.update(status='REVIEW_REQUIRED',phase='Compatible bulk baseline complete; review precision and pressure',source_unchanged=True,
                     pressure_followups=sum(r['classification']=='followup' for r in pressure),
                     pressure_inconclusive=sum(r['classification'].startswith('inconclusive') for r in pressure))
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
            plan,_,_=prepare(RUN)
            print(json.dumps({k:plan[k] for k in ('stage','new_cases_to_run','inherited_used_seconds','forecast')},indent=2));return 0
        b.RUN=RUN;b.base.RUN=RUN;scan.launch_command=launch
        return execute()


if __name__=='__main__':raise SystemExit(main())
