#!/usr/bin/env python3
"""G5-65R4: ten production workers, temperature telemetry without software cutoff."""
from datetime import datetime
import hashlib
import heapq
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixture_pressure_65_temperature as previous

c, r, thermal = previous.c, previous.r, previous.thermal
RUN = ROOT/'runs/noble_cf4_mixture_pressure_65_ten'
PRIOR = ROOT/'runs/noble_cf4_mixture_pressure_65_temperature'
UNIT = 'srim-mixture-pressure-65-ten.service'
WORKERS = 10


def snapshot():
    sample = thermal.thermal_snapshot()
    sample.pop('limits_c')  # Older95C derived thresholds are not active in this run.
    sample['software_temperature_cutoff_c'] = None
    return sample


def check(sample, baseline, last=None):
    for field in ('temperatures_c', 'throttle_counts', 'critical_c', 'reported_high_c'):
        if sample[field].keys() != baseline[field].keys():
            raise RuntimeError('Thermal telemetry set changed')
    for field in ('critical_c', 'reported_high_c'):
        if sample[field] != baseline[field]:
            raise RuntimeError('Hardware thermal thresholds changed')
    reference = last if last is not None else baseline
    if any(not isinstance(v, int) or v < reference['throttle_counts'][k]
           for k, v in sample['throttle_counts'].items()):
        raise RuntimeError('Thermal counter invalid or reset')
    # No startup-temperature or high-temperature stop; hardware controls are untouched.


def review_stop(accepted):
    plan = c.read(PRIOR, 'plan.json'); r.old.gate.verify_hashes(plan['input_sha256'])
    state, guard, budget, exited = (c.read(PRIOR, n) for n in
        ('status.json', 'thermal-status.json', 'budget.json', 'service-exit.json'))
    if (state['status'] != 'NEEDS_ATTENTION' or state['completed_continuation_batches'] != 0
            or guard['status'] != 'NEEDS_ATTENTION'
            or guard['error'] != 'Thermal guard: CPU sensor 96.0 C reached stop threshold'
            or (PRIOR/'completion.json').exists() or list((PRIOR/'cases').glob('*/result.json'))
            or c.read(PRIOR, 'results.json') != accepted
            or [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['exit-code', 'exited', '1']):
        raise ValueError('Previous stop or accepted production changed')
    samples = [json.loads(line) for line in (PRIOR/'thermal-samples.jsonl').read_text().splitlines()]
    for sample in samples:
        previous.temperature_check(sample, guard['baseline'])
    try:
        previous.temperature_check(guard['latest'], guard['baseline'])
    except RuntimeError as exc:
        if str(exc) != guard['error']:
            raise
    else:
        raise ValueError('Missing recorded temperature stop')
    elapsed = (datetime.fromisoformat(exited['ended_at'])-datetime.fromisoformat(guard['started_at'])).total_seconds()
    if (len(samples)+1 != guard['samples'] or budget['limit_seconds'] != r.BUDGET
            or budget['used_seconds'] < plan['inherited_used_seconds']+elapsed-.001):
        raise ValueError('Previous thermal trace or cumulative budget incomplete')
    paths = {PRIOR/n for n in ('plan.json', 'status.json', 'results.json', 'thermal-status.json', 'thermal-samples.jsonl',
                              'throttle-warnings.jsonl', 'budget.json', 'service-exit.json', 'session.json', 'logs/campaign.log')}
    paths.update(x for x in (PRIOR/'cases').rglob('*') if x.is_file())
    for runtime in (PRIOR/'runtime').glob('worker-*'):
        paths.update(r.scan.collect_runtime_outputs(runtime).values())
        paths.update(x for x in runtime.iterdir() if x.is_file() and
                     (x.name.upper() in ('TRIM.IN', 'TRIM.DAT') or x.name.startswith('.')))
    manifest = dict(plan['input_sha256'])
    manifest.update({str(x): r.p.history.actual_sha256(x) for x in sorted(paths)})
    return plan, budget['used_seconds'], manifest


def forecast(pending):
    loads = [0.]*WORKERS
    for case in sorted(pending, key=lambda x:x['estimated_seconds'], reverse=True):
        heapq.heapreplace(loads, loads[0]+case['estimated_seconds'])
    model = max(loads)/3600
    return dict(model_hours=model, planning_hours=1.5*model+.5+len(pending)/3600,
                phase_model_hours=dict(pressure_production=model),
                longest_estimated_batch_hours=max(c['estimated_seconds'] for c in pending)/3600,
                method='Original measured batch-cost estimates scheduled longest-first on10workers; '
                       '50%margin plus30min and1s perlaunch. Not measured sustained10-worker throughput; '
                       'hardware throttling may increase actual wall time. No scaled physics results.')


def prepare():
    frozen, inputs, baseline, accepted, _, _ = r.review.review()
    old, used, manifest = review_stop(accepted)
    pending = [case for case in frozen['cases'] if case['name'] not in old['retained_original_names']]
    if (len(accepted),len(baseline),len(pending)) != (251,4536,1009) or frozen['cases'] != old['cases']:
        raise ValueError('Original cohort changed')
    estimate = forecast(pending)
    if used/3600+estimate['planning_hours']+(r.JOB_TIMEOUT+90)/3600 > 24:
        raise ValueError('Insufficient original budget')
    for case in pending:
        for name, content, digest in zip(('TRIM.IN','TRIM.DAT'),inputs[case['name']],
                                        (case['input_sha256'],case['trim_dat_sha256'])):
            path = RUN/'inputs'/case['name']/name
            if hashlib.sha256(content).hexdigest() != digest or (path.exists() and path.read_bytes() != content):
                raise ValueError('Frozen input changed')
            if not path.exists():
                path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(content)
            manifest[str(path)] = digest
    for path in (Path(__file__), ROOT/'scripts/mixture_pressure_65_ten.sh'):
        manifest[str(path)] = r.p.history.actual_sha256(path)
    policy = dict(software_temperature_cutoff_c=None, startup_temperature_cutoff_c=None,
                  stop_on_new_throttling=False, warn_on_new_throttling=True,
                  hardware_protections_unchanged=True, temperature_guarantee=False,
                  stop_on_telemetry_loss_or_reset=True, poll_seconds=.2)
    plan = dict(old,stage='G5-65R4',workers=WORKERS,input_sha256=manifest,inherited_used_seconds=used,
                previous_failed_run=str(PRIOR),forecast=estimate,thermal_policy=policy,
                user_direction='User explicitly requested only10workers andno temperature cap. '
                               'Remove softwaretemperature stops,not hardwareprotections;production only,no loadtest.')
    if (RUN/'plan.json').exists() and c.read(RUN,'plan.json') != plan:
        raise ValueError('Frozen continuation changed')
    if not (RUN/'plan.json').exists():
        c.write_json(RUN/'plan.json',plan)
    if not (RUN/'budget.json').exists():
        c.write_json(RUN/'budget.json',dict(used_seconds=used,limit_seconds=r.BUDGET))
    budget = c.read(RUN,'budget.json')
    if budget['used_seconds'] < used or budget['limit_seconds'] != r.BUDGET:
        raise ValueError('Inherited budget reset')
    return plan,inputs,baseline,accepted


def publish(state,rows,baseline):
    state.update(stage='G5-65R4',worker_process_limit=10,cpu_quota_percent=1000,
                 software_temperature_cutoff_c=None,stop_on_new_throttling=False,
                 hardware_protections_unchanged=True,load_test_skipped_by_user=True)
    if state.get('status') == 'REVIEW_REQUIRED':
        state['phase'] = 'G5-65R4 complete; review before G5-70'
    c.original_publish(state,rows,baseline)
    report = RUN/'index.html'
    r.atomic_text(report,report.read_text().replace('15cores,','10workers; no softwaretemperature cutoff,'))


def guard():
    group = Path(r.resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) != WORKERS
            or (group/'cpu.max').read_text().strip() != c.CPU_MAX
            or (group/'memory.max').read_text().strip() != str(32*r.GIB)
            or (group/'memory.swap.max').read_text().strip() != '0'
            or any(os.environ.get(k) != v for k,v in thermal.THREAD_ENV.items())):
        raise RuntimeError('Use bounded10-worker launcher')
    if (RUN/'status.json').exists() or (RUN/'thermal-status.json').exists():
        raise RuntimeError('Already attempted; no automatic retry')
    used = c.read(RUN,'plan.json')['inherited_used_seconds']
    if used >= r.BUDGET or c.read(RUN,'budget.json') != dict(used_seconds=used,limit_seconds=r.BUDGET):
        raise ValueError('Invalid inherited budget')
    start = time.monotonic(); deadline = start+r.BUDGET-used
    state = dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),
                 inherited_used_seconds=used,samples=0,software_temperature_cutoff_c=None,
                 hardware_protections_unchanged=True,worker_process_limit=10,throttle_warning_samples=0)
    process = None
    def stop(*_):
        raise InterruptedError('Service stop requested')
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,stop)
    try:
        baseline = snapshot(); check(baseline,baseline); last = baseline; last_warning = -float('inf')
        state.update(baseline=baseline,peak_temperature_c=max(baseline['temperatures_c'].values()))
        c.write_json(RUN/'thermal-status.json',state)
        c.write_json(RUN/'session.json',dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                                            cpu_affinity=sorted(os.sched_getaffinity(0)),thread_environment=thermal.THREAD_ENV))
        process = subprocess.Popen([sys.executable,'-X','faulthandler',__file__,'run','--deadline',str(deadline)],cwd=ROOT)
        with (RUN/'thermal-samples.jsonl').open('x') as stream:
            while True:
                current = snapshot(); check(current,baseline,last)
                state.update(samples=state['samples']+1,latest=current,
                             peak_temperature_c=max(state['peak_temperature_c'],max(current['temperatures_c'].values())))
                if current['throttle_counts'] != last['throttle_counts']:
                    state['throttle_warning_samples'] += 1
                    if time.monotonic()-last_warning >= 30:
                        warning = dict(time=current['time'],warning='Thermal throttling observed;no softwaretemperature cutoff',
                                       sampled_max_c=max(current['temperatures_c'].values()))
                        with (RUN/'throttle-warnings.jsonl').open('a') as log:
                            log.write(json.dumps(warning)+'\n')
                        print(json.dumps(warning),flush=True); last_warning = time.monotonic()
                stream.write(json.dumps(current)+'\n'); stream.flush(); last = current
                c.write_json(RUN/'thermal-status.json',state)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Original cumulative24h budget exhausted')
                code = process.poll()
                if code is not None:
                    if code or c.read(RUN,'status.json') != c.read(RUN,'completion.json') or c.read(RUN,'status.json')['status'] != 'REVIEW_REQUIRED':
                        raise RuntimeError(f'Controller exited{code} without valid completion')
                    break
                time.sleep(.2)
        state.update(status='COMPLETE',child_returncode=0); return 0
    except Exception as exc:
        state['stopped_worker_pids'] = thermal.kill_owned_workers(group)
        if process is not None:
            process.wait(timeout=10)
        state.update(status='NEEDS_ATTENTION',error=str(exc))
        status = c.read(RUN,'status.json') if (RUN/'status.json').exists() else {}
        status.update(status='NEEDS_ATTENTION',guard_error=str(exc)); c.write_json(RUN/'status.json',status)
        r.atomic_text(RUN/'index.html','<html><h1>G5-65R4 needs attention</h1>'+r.table([status],list(status)))
        print(str(exc),flush=True); return 1
    finally:
        state.update(ended_at=datetime.now().astimezone().isoformat(),wall_seconds=time.monotonic()-start)
        c.write_json(RUN/'thermal-status.json',state)
        c.write_json(RUN/'budget.json',dict(used_seconds=used+time.monotonic()-start,limit_seconds=r.BUDGET))


def bind():
    c.RUN,c.UNIT = RUN,UNIT; c.bind()
    r.__file__ = __file__; r.WORKERS = WORKERS
    r.prepare,r.publish,r.guard = prepare,publish,guard


if __name__ == '__main__':
    bind()
    raise SystemExit(r.main())
