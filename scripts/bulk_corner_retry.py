#!/usr/bin/env python3
"""C2d: preserve 19 audited corner cases and retry high energies with atomic Xvfb."""
from __future__ import annotations
import argparse
import copy
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bulk_corner_validation as base
from scripts import private_xvfb as display
from scripts import display_allocation_test as stress
from scripts.staged_scan import write_json
from scripts.preliminary_report import atomic_text
scan = base.scan
OLD_RUN = base.RUN
RUN = ROOT / 'runs/noble_cf4_bulk_corners_retry'
UNIT = 'srim-bulk-corners-retry.service'
old_prepare, old_run_case, old_publish = base.prepare, base.run_case, base.publish
old_launch = scan.launch_command


def review_failed():
    plan, inputs = old_prepare(OLD_RUN)
    state = json.loads((OLD_RUN / 'status.json').read_text())
    if state['status'] != 'NEEDS_ATTENTION' or state['completed_cases'] != 19:
        raise ValueError('Expected preserved 19-case canary completion followed by display failure')
    records = json.loads((OLD_RUN / 'results.json').read_text())
    if len(records) != 34 or len({r['name'] for r in records}) != 34: raise ValueError('Unexpected failed-run records')
    cases = {c['name']: c for c in plan['cases']}; accepted = []
    for r in records:
        saved = json.loads((OLD_RUN / 'cases' / r['name'] / 'result.json').read_text())
        if saved != r or any(r[k] != v for k,v in cases[r['name']].items()): raise ValueError('Case identity/index mismatch')
        for path,digest in r['artifact_sha256'].items():
            if scan.sha256_file(Path(path)) != digest: raise ValueError('Old raw evidence changed')
        if r['status'] != 'COMPLETE': continue
        directory = Path(r['attempt']); content, events = inputs[r['name']]
        if (directory/'TRIM.IN').read_bytes()!=content or (directory/'TRIM.DAT').read_bytes()!=events:
            raise ValueError('Executed input mismatch in retained case')
        fresh = base.analyse(cases[r['name']], {p.name.upper():p for p in directory.iterdir()})
        if any(r[k]!=v for k,v in fresh.items()): raise ValueError('Retained raw analysis mismatch')
        ack=json.loads((directory/'completion-ack.json').read_text())
        progress=json.loads((directory/'.trim-progress.json').read_text())
        if ack['status']!='sent' or ack['title']!=base.completion.TITLE or progress['status']!='complete':
            raise ValueError('Retained completion lifecycle evidence missing')
        accepted.append(r)
    failed=[r for r in records if r['status']=='FAILED']
    if len(failed)!=1 or 'TRIM_WATCHDOG_ERROR' not in failed[0]['error'] or 'unable to open display' not in failed[0]['error']:
        raise ValueError('Failure is not the diagnosed display startup issue')
    failed_path=Path(failed[0]['attempt'])
    # The old runner cleared progress before launch, but not its previous empty
    # process log. That stale log is preserved, not treated as a new TRIM start.
    if (failed_path/'.trim-progress.json').exists() or 'line 100, in monitored_trim_run' not in failed[0]['error']:
        raise ValueError('Failed worker unexpectedly launched TRIM; investigate separately')
    log=(failed_path/'.xvfb.log').read_text()
    if log.count('Server is already active for display') < 10:
        raise ValueError('Missing display-allocation retry-exhaustion evidence')
    checks=base.depth_checks(accepted)
    if len(accepted)!=19 or len(checks)!=9 or not all(r['passed'] for r in checks): raise ValueError('Retained low-energy gate incomplete')
    if any(r['phase']!='canary' for r in accepted): raise ValueError('Unexpected retained high-energy result')
    budget=json.loads((OLD_RUN/'budget.json').read_text())
    summary=dict(status='AUDITED_DISPLAY_ALLOCATION_FAILURE', complete_cases=19, failed_cases=1, stopped_cases=14,
                 unlaunched_cases=3, all_low_energy_depth_pairs_passed=True, exact_replay_passed=True,
                 max_low_energy_depth_qf_relative_change=max(abs(r['qf_relative_change']) for r in checks),
                 max_low_energy_depth_dx_relative_change=max(abs(r['dx_relative_change']) for r in checks),
                 inherited_used_seconds=budget['used_seconds'], resources=state['resources'],
                 cause='Concurrent xvfb-run -a starts exhausted its ten display-number attempts; '
                       'the script then invoked the worker on an unavailable display. The watchdog rejected it before TRIM launch. '
                       'Fourteen other high-energy attempts were interrupted, three never launched; no high-energy output accepted.',
                 decision='Use Xvfb -displayfd for server-owned allocation and explicit authenticated readiness. '
                          'Retain exactly 19 audited results, retry the same 18 high-energy inputs/seeds in a new run, '
                          'carry forward spent time under the original six-hour cumulative cap; production remains held.')
    return plan, inputs, accepted, records, summary


def prepare(run):
    old, inputs, retained, records, review = review_failed()
    test=json.loads((stress.RUN/'result.json').read_text())
    test_plan=json.loads((stress.RUN/'plan.json').read_text())
    if test['status']!='COMPLETE' or test['completed_displays']!=60 or len(test['checks'])!=60:
        raise ValueError('Concurrent display stress test not complete')
    for path,digest in {**test_plan['input_sha256'],**test['artifact_sha256']}.items():
        if scan.sha256_file(Path(path))!=digest: raise ValueError('Display test provenance changed')
    for wave in range(4):
        rows=[r for r in test['checks'] if r['wave']==wave]
        if len(rows)!=15 or len({r['display'] for r in rows})!=15 or any(not r['server_reaped'] or not r['unauthorized_client_rejected'] for r in rows):
            raise ValueError('Display concurrency/isolation/cleanup gate failed')
    paths={Path(__file__),ROOT/'scripts/bulk_corner_retry.sh',Path(display.__file__),Path('/usr/bin/xvfb-run'),
           OLD_RUN/'plan.json',OLD_RUN/'status.json',OLD_RUN/'results.json',OLD_RUN/'budget.json',
           stress.RUN/'plan.json',stress.RUN/'result.json'}
    for r in records:
        paths.add(OLD_RUN/'cases'/r['name']/'result.json'); paths.update(Path(p) for p in r['artifact_sha256'])
    plan=copy.deepcopy(old)
    plan['input_sha256'].update({str(p):scan.sha256_file(p) for p in sorted(paths)})
    plan.update(stage='C2d', retained_cases=[r['name'] for r in retained], new_cases_to_run=18,
                previous_failed_run=str(OLD_RUN), failure_review=review,
                inherited_used_seconds=review['inherited_used_seconds'],
                display_fix='Atomic Xvfb -displayfd allocation, authenticated root-window readiness, private authority, '
                            'TCP disabled, lifecycle cleanup and startup metadata archived. Original physics/seed/gates unchanged.',
                forecast=dict(old['forecast'], planning_hours=1.5*old['forecast']['phase_model_hours']['high_energy']+.5,
                              note='Only eighteen unfinished high-energy cases run; nineteen audited canaries reused. '
                                   'Original spent time carries forward under the six-hour cumulative limit.'))
    path=run/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan: raise ValueError('Frozen corner retry changed')
    if not path.exists():write_json(path,plan)
    write_json(run/'previous_review.json',review)
    for r in retained:
        target=run/'cases'/r['name']/'result.json'; value=dict(r,reused_from=str(OLD_RUN/'cases'/r['name']/'result.json'))
        if target.exists() and json.loads(target.read_text())!=value: raise ValueError('Retained case changed')
        if not target.exists():write_json(target,value)
    budget=run/'budget.json'
    if not budget.exists():write_json(budget,dict(used_seconds=review['inherited_used_seconds'],limit_seconds=base.BUDGET))
    elif json.loads(budget.read_text())['used_seconds'] < review['inherited_used_seconds']:
        raise ValueError('Retry reset inherited budget')
    return plan,inputs


def display_launch(command,cwd,timeout_s,*args,**kwargs):
    expected=['xvfb-run','-a','-e',str(cwd/'.xvfb.log'),'-s','-screen 0 1024x768x24']
    if list(command[:6])!=expected or list(command[6:9])!=[sys.executable,str(Path(base.__file__).resolve()),'worker']:
        raise ValueError('Unexpected worker command; refuse implicit launch rewrite')
    rewritten=[sys.executable,str(Path(display.__file__).resolve()),'--runtime',str(cwd),'--',*command[6:]]
    return old_launch(rewritten,cwd,timeout_s,*args,**kwargs)


def run_case(case,contents,runtime,context,deadline):
    (runtime/'.display-startup.json').unlink(missing_ok=True)  # Previously archived worker scratch only.
    (runtime/'.trim-process.log').unlink(missing_ok=True)
    result=old_run_case(case,contents,runtime,context,deadline)
    metadata=runtime/'.display-startup.json'
    if metadata.exists():
        target=Path(result['attempt'])/metadata.name;shutil.copy2(metadata,target)
        result['artifact_sha256'][str(target)]=scan.sha256_file(target)
        result['display_startup']=json.loads(target.read_text())
    if result['status']=='COMPLETE' and result.get('display_startup',{}).get('allocation')!='Xvfb -displayfd':
        result.update(status='FAILED',error='Missing atomic display-allocation evidence')
    write_json(RUN/'cases'/case['name']/'result.json',result)
    return result


def publish(state,results):
    old_publish(state,results)
    path=RUN/'index.html';text=path.read_text().replace('C2c:','C2d:').replace('SRIM C2c','SRIM C2d')
    text=text.replace('<p>37 fixed','<p>Display-allocation retry: 19 completed canaries are retained with original raw hashes; '
                      'only 18 high-energy cases are rerun. The failed attempt remains preserved. '
                      'All 60 concurrent display stress checks passed; six-hour cumulative budget includes the previous run.</p><p>37 fixed')
    atomic_text(path,text)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','run','status'));args=parser.parse_args()
    if args.command=='status':
        subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'])
        if (RUN/'status.json').exists():print((RUN/'status.json').read_text())
        print('Report:',RUN/'index.html');return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,_=prepare(RUN);print(json.dumps({k:plan[k] for k in ('stage','new_cases_to_run','inherited_used_seconds','forecast')},indent=2));return 0
        # Process-local adapter: leave every historical hashed source unchanged.
        base.RUN,base.UNIT=RUN,UNIT
        base.prepare,base.run_case,base.publish=prepare,run_case,publish
        scan.launch_command=display_launch
        return base.execute()


if __name__=='__main__':raise SystemExit(main())
