#!/usr/bin/env python3
"""G5-65R: exact fixed-cohort continuation with isolated workers and thermal guard."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import faulthandler
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import review_supervisor_crash_gate as review
from scripts import crash_gate_thermal_guard as thermal
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

g = review.g
old, p, b, scan = g.old, g.p, g.b, g.scan
RUN = ROOT/'runs/noble_cf4_mixture_pressure_65_isolated'
UNIT = 'srim-mixture-pressure-65-isolated.service'
WORKERS, BUDGET, JOB_TIMEOUT = old.WORKERS, old.BUDGET, old.JOB_TIMEOUT


def load(name):
    return json.loads((RUN/name).read_text())


def prepare():
    frozen, inputs, baseline, accepted, summary, manifest = review.review()
    names = sorted(r['name'] for r in accepted)
    pending = [c for c in frozen['cases'] if c['name'] not in names]
    forecast = old.forecast(pending)
    if (len(pending) != 1009 or any(c['phase'] != 'pressure_production' for c in pending)
            or summary['inherited_used_seconds']/3600+forecast['planning_hours']+(JOB_TIMEOUT+90)/3600 > 24):
        raise ValueError('Continuation cohort or inherited time budget changed')
    paths = [Path(__file__), ROOT/'scripts/mixture_pressure_65_isolated.sh', Path(review.__file__)]
    manifest.update({str(x): p.history.actual_sha256(x) for x in paths})
    # Persist exact bytes once, for inexpensive dedicated-worker startup.
    for case in pending:
        for name, content, digest in zip(('TRIM.IN', 'TRIM.DAT'), inputs[case['name']],
                                        (case['input_sha256'], case['trim_dat_sha256'])):
            path = RUN/'inputs'/case['name']/name
            if hashlib.sha256(content).hexdigest() != digest or (path.exists() and path.read_bytes() != content):
                raise ValueError('Original input bytes changed')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            manifest[str(path)] = digest
    plan = dict(frozen, stage='G5-65R', input_sha256=manifest, retained_original_names=names,
                remaining_batches=1009, inherited_used_seconds=summary['inherited_used_seconds'],
                forecast=forecast, recovery_review=summary, previous_failed_run=str(old.RUN),
                worker_policy='one dedicated process per batch; single-threaded controller; no automatic retries',
                thermal_policy=dict(start_below_c=75, stop_at_c=85, use_lower_hardware_high_limit=True,
                                    stop_on_throttle_counter_change=True, sample_seconds=.5),
                manual_callback_before_next_wave=True)
    path = RUN/'plan.json'
    if path.exists() and load('plan.json') != plan:
        raise ValueError('Frozen isolated continuation changed')
    if not path.exists():
        write_json(path, plan)
    write_json(RUN/'previous_review.json', summary)
    budget = RUN/'budget.json'
    if not budget.exists():
        write_json(budget, dict(used_seconds=summary['inherited_used_seconds'], limit_seconds=BUDGET))
    elif load('budget.json')['used_seconds'] < summary['inherited_used_seconds'] or load('budget.json')['limit_seconds'] != BUDGET:
        raise ValueError('Inherited budget was reset')
    return plan, inputs, baseline, accepted


def audit_case(case, row, contents):
    if row.get('diagnostic') or row.get('status') != 'COMPLETE' or any(row.get(k) != v for k, v in case.items()):
        raise ValueError('Production case identity or completion mismatch')
    old.original_audit(case, row, contents)
    if (not Path(row['attempt']).is_relative_to(RUN/'cases'/case['name'])
            or row['display_startup'].get('reset_on_last_client') is not False
            or row['display_startup'].get('helper') != str(Path(old.gate.fixed_display.__file__))):
        raise ValueError('Unowned attempt or missing validated noreset lifecycle')


def child(name, runtime, deadline):
    plan = load('plan.json')
    case = next(c for c in plan['cases'] if c['name'] == name)
    if name in plan['retained_original_names']:
        raise ValueError('Cannot rerun accepted production')
    contents = tuple((RUN/'inputs'/name/n).read_bytes() for n in ('TRIM.IN', 'TRIM.DAT'))
    if [hashlib.sha256(x).hexdigest() for x in contents] != [case['input_sha256'], case['trim_dat_sha256']]:
        raise ValueError('Frozen worker input changed')
    p.RUN = b.RUN = b.base.RUN = RUN
    p.audit_case, p.check_composition, scan.launch_command = audit_case, old.gate.retry.check_composition, old.gate.launch
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: scan.SHUTDOWN_REQUESTED.set())
    context = scan.resolve_execution_context(scan.load_config(b.base.d.CONFIG))
    row = p.run_case(case, contents, runtime, scan.runtime_execution_context(context, RUN, runtime), deadline)
    return 0 if row['status'] == 'COMPLETE' else 1


@contextmanager
def report_context():
    saved = old.RUN; old.RUN = RUN
    try:
        yield
    finally:
        old.RUN = saved


def publish(state, rows, baseline):
    state.update(retained_original_batches=251, completed_continuation_batches=max(0, len(rows)-251),
                 expected_continuation_batches=1009, diagnostic_histories_excluded=750,
                 root_cause_proven=False, worker_policy='isolated processes',
                 thermal_guard_required_for_release=True)
    with report_context():
        old.publish(state, rows, baseline)


def launch_case(case, runtime, deadline):
    path = RUN/'logs'/(case['name']+'.log')
    with path.open('xb') as stream:
        return subprocess.Popen([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'child',
            '--case', case['name'], '--runtime', str(runtime), '--deadline', str(deadline)],
            stdout=stream, stderr=subprocess.STDOUT, env=dict(os.environ, PYTHONFAULTHANDLER='1'))


def run_batches(pending, runtimes, deadline, accept, heartbeat):
    """Only exit-zero, independently re-audited children enter the accepted index."""
    pending = sorted(pending, key=lambda c: c['estimated_seconds'], reverse=True)
    available, active = list(runtimes), {}
    try:
        while pending or active:
            if scan.SHUTDOWN_REQUESTED.is_set() or time.monotonic() >= deadline:
                raise InterruptedError('Stopped or cumulative budget exhausted')
            while pending and available:
                if deadline-time.monotonic() < JOB_TIMEOUT+90:
                    break  # Drain active workers, but do not dispatch another batch.
                case, runtime = pending.pop(0), available.pop()
                process = launch_case(case, runtime, deadline)
                active[process] = (case, runtime, time.monotonic())
            for process, (case, runtime, started) in list(active.items()):
                code = process.poll()
                if code is not None:
                    if code != 0:
                        raise RuntimeError(f'Worker {case["name"]} exited {code}; no retry or further dispatch')
                    accept(case); del active[process]; available.append(runtime)
                elif time.monotonic()-started > JOB_TIMEOUT+90:
                    raise TimeoutError('Isolated worker exceeded job/drain cap: '+case['name'])
            heartbeat()
            if pending and not active and deadline-time.monotonic() < JOB_TIMEOUT+90:
                raise InterruptedError('Insufficient original cumulative job/drain reserve')
            if active:
                time.sleep(.5)
    finally:
        for process in active:
            if process.poll() is None:
                process.terminate()
        end = time.monotonic()+25
        for process in active:
            try:
                process.wait(timeout=max(.1, end-time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


def execute(deadline):
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS
            or (group/'memory.max').read_text().strip() != str(32*GIB)
            or (group/'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bounded isolated launcher')
    state = dict(status='RUNNING', stage='G5-65R', phase='auditing_previous_gate',
                 started_at=datetime.now().astimezone().isoformat())
    rows, baseline = [], []; last_update = -math.inf
    def stop(*_):
        scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    def update(force=False):
        nonlocal last_update
        if not force and time.monotonic()-last_update < 5:
            return
        last_update = time.monotonic()
        used = BUDGET-(deadline-time.monotonic())
        state.update(updated_at=datetime.now().astimezone().isoformat(), cumulative_hours=used/3600,
                     resources=resource_sample())
        write_json(RUN/'budget.json', dict(used_seconds=used, limit_seconds=BUDGET))
        publish(state, rows, baseline)
    try:
        update(True); plan, inputs, baseline, rows = prepare()
        if scan.SHUTDOWN_REQUESTED.is_set() or memory_info()['MemAvailable'] < 16*GIB:
            raise InterruptedError('Stop requested or insufficient available RAM')
        names = set(plan['retained_original_names'])
        pending = [c for c in plan['cases'] if c['name'] not in names]
        context = scan.resolve_execution_context(scan.load_config(b.base.d.CONFIG))
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, scan.load_config(b.base.d.CONFIG)), RUN, WORKERS, context)
        write_json(RUN/f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        state.update(phase='pressure_production', low_pressure_gate_passed=True, high_pressure_gate_passed=True)
        update(True)
        def accept(case):
            row = load('cases/'+case['name']+'/result.json')
            with old.gate.retry.parser_context():
                audit_case(case, row, inputs[case['name']])
            rows.append(row); update(True)
            print(json.dumps(dict(name=case['name'], status='COMPLETE', wall_seconds=row['wall_seconds'])), flush=True)
        run_batches(pending, runtimes, deadline, accept, update)
        state['phase'] = 'final_raw_audit_and_pressure_screen'; update(True)
        old.gate.verify_hashes(plan['input_sha256'])
        indexed = {r['name']: r for r in rows}
        if len(rows) != 1260 or len(indexed) != 1260:
            raise ValueError('Incomplete continuation cohort')
        with old.gate.retry.parser_context():
            for case in plan['cases']:
                row = indexed[case['name']]
                if case['name'] in names:
                    if row != json.loads((old.RUN/'cases'/case['name']/'result.json').read_text()):
                        raise ValueError('Retained production changed')
                    old.audit_case(case, row, inputs[case['name']])
                else:
                    audit_case(case, row, inputs[case['name']])
        points = b.p.point_summary(baseline+rows)
        if len(points) != 1449 or not all(x['complete'] for x in points) or len({r['seed'] for r in baseline+rows}) != 5796:
            raise ValueError('Combined point/seed accounting changed')
        checks = old.pressure_checks(baseline+rows, plan['pressure_screen'])
        write_json(RUN/'pressure_checks.json', checks)
        atomic_text(RUN/'pressure_checks.csv', csv_text(checks, sorted({k for x in checks for k in x})))
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise ValueError('OOM observed')
        with report_context():
            old.plots(points)
        if scan.SHUTDOWN_REQUESTED.is_set() or time.monotonic() >= deadline:
            raise InterruptedError('Stopped or budget exhausted during final audit')
        state.update(status='REVIEW_REQUIRED', phase='G5-65R complete; review before G5-70', source_unchanged=True,
                     pressure_production_passed=True, combined_independent_batches=5796, combined_histories=289800,
                     pressure_followup_intervals=sum(x['classification'] == 'followup' for x in checks),
                     longest_batch_seconds=max(r['wall_seconds'] for r in rows))
        update(True); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='NEEDS_ATTENTION', error=str(exc)); update(True)
        print(str(exc), file=sys.stderr, flush=True); return 1


def guard():
    if (RUN/'status.json').exists() or (RUN/'thermal-status.json').exists():
        raise RuntimeError('Already attempted; review before any versioned retry')
    plan = load('plan.json'); used = load('budget.json')['used_seconds']
    if used < plan['inherited_used_seconds'] or used >= BUDGET:
        raise ValueError('Invalid inherited budget')
    start = time.monotonic(); deadline = start+BUDGET-used
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), inherited_used_seconds=used, samples=0)
    process = None
    def stop(*_):
        raise InterruptedError('Guard interrupted')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        baseline = thermal.snapshot(); thermal.check(baseline, baseline, initial=True)
        state.update(baseline=baseline, peak_temperature_c=max(baseline['temperatures_c'].values()))
        write_json(RUN/'thermal-status.json', state)
        process = subprocess.Popen([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'run',
                                    '--deadline', str(deadline)], start_new_session=True)
        with (RUN/'thermal-samples.jsonl').open('x') as stream:
            while True:
                current = thermal.snapshot(); stream.write(json.dumps(current)+'\n'); stream.flush()
                state.update(samples=state['samples']+1, latest=current,
                             peak_temperature_c=max(state['peak_temperature_c'], max(current['temperatures_c'].values())))
                thermal.check(current, baseline)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Original cumulative24h budget exhausted')
                write_json(RUN/'thermal-status.json', state)
                code = process.poll()
                if code is not None:
                    if code:
                        raise RuntimeError(f'Isolated controller exited {code}; inspect campaign/worker logs')
                    if load('status.json') != load('completion.json') or load('status.json')['status'] != 'REVIEW_REQUIRED':
                        raise ValueError('Controller exit zero without reviewed completion')
                    break
                time.sleep(.5)
        state.update(status='COMPLETE', child_returncode=0)
        return 0
    except Exception as exc:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        state.update(status='NEEDS_ATTENTION', error=str(exc))
        status = load('status.json') if (RUN/'status.json').exists() else {}
        status.update(status='NEEDS_ATTENTION', guard_error=str(exc))
        write_json(RUN/'status.json', status)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>G5-65R needs attention</h1>'+table([status], list(status)))
        print(str(exc), flush=True); return 1
    finally:
        state.update(ended_at=datetime.now().astimezone().isoformat(), wall_seconds=time.monotonic()-start)
        write_json(RUN/'thermal-status.json', state)
        write_json(RUN/'budget.json', dict(used_seconds=used+time.monotonic()-start, limit_seconds=BUDGET))


def finalize():
    ended = datetime.now().astimezone()
    evidence = dict(ended_at=ended.isoformat(), service_result=os.environ.get('SERVICE_RESULT'),
                    exit_code=os.environ.get('EXIT_CODE'), exit_status=os.environ.get('EXIT_STATUS'))
    write_json(RUN/'service-exit.json', evidence)
    if (RUN/'thermal-status.json').exists():
        thermal_state = load('thermal-status.json')
        used = thermal_state['inherited_used_seconds']+(ended-datetime.fromisoformat(thermal_state['started_at'])).total_seconds()
        write_json(RUN/'budget.json', dict(used_seconds=max(used, load('budget.json')['used_seconds']), limit_seconds=BUDGET))
        if thermal_state['status'] == 'RUNNING':
            thermal_state.update(status='NEEDS_ATTENTION', service_exit=evidence)
            write_json(RUN/'thermal-status.json', thermal_state)
    if (RUN/'status.json').exists():
        state = load('status.json')
        if state['status'] == 'RUNNING' or evidence['service_result'] != 'success':
            state.update(status='NEEDS_ATTENTION', service_exit=evidence)
            write_json(RUN/'status.json', state)
            atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>G5-65R needs attention</h1>'+table([state], list(state)))


def main():
    faulthandler.enable(all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'guard', 'run', 'child', 'status', 'finalize'))
    parser.add_argument('--case'); parser.add_argument('--runtime', type=Path); parser.add_argument('--deadline', type=float)
    args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        for name in ('status.json', 'thermal-status.json', 'budget.json'):
            if (RUN/name).exists():
                print(json.dumps({k: v for k, v in load(name).items() if k not in ('baseline', 'latest')}, indent=2))
        print('Report:', RUN/'index.html'); return 0
    if args.command == 'finalize':
        finalize(); return 0
    if args.command == 'child':
        return child(args.case, args.runtime, args.deadline)
    if args.command == 'run':
        return execute(args.deadline)
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'guard':
            return guard()
        plan, *_ = prepare()
        print(json.dumps({k: plan[k] for k in ('stage', 'remaining_batches', 'inherited_used_seconds', 'forecast')}, indent=2))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
