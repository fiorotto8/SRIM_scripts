#!/usr/bin/env python3
"""C1g: fixed15-worker exact-replay validation of the user-amended thermal policy."""
import argparse
from datetime import datetime
import faulthandler
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixture_pressure_65_isolated as r
from scripts.preliminary_report import atomic_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

g, old, p, b, scan = r.g, r.old, r.p, r.b, r.scan
RUN = ROOT/'runs/thermal_95_gate'
UNIT = 'srim-thermal-95-gate.service'
BUDGET, WORKERS, POLL = 1800, 15, .2
THREAD_ENV = {k: '1' for k in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                              'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'BLIS_NUM_THREADS')}
INTEL_URL = 'https://www.intel.com/content/www/us/en/products/sku/241068/intel-core-ultra-7-processor-265-30m-cache-up-to-5-30-ghz/specifications.html'


def read(name):
    return json.loads((RUN/name).read_text())


def thermal_snapshot():
    sample = r.thermal.snapshot()
    if any(not 0 < value < 150 for value in sample['temperatures_c'].values()):
        raise RuntimeError('Invalid CPU temperature telemetry')
    # Read thresholds only; never change hardware/OS thermal protections.
    sample['reported_high_c'] = sample.pop('limits_c')
    sample['critical_c'] = {s: int(Path(s).with_name(Path(s).name.replace('_input', '_crit')).read_text())/1000
                            for s in sample['temperatures_c']}
    sample['limits_c'] = {s: min(95., limit-5.) for s, limit in sample['critical_c'].items()}
    return sample


def thermal_check(sample, baseline, initial=False):
    if sample['critical_c'] != baseline['critical_c'] or sample['reported_high_c'] != baseline['reported_high_c']:
        raise RuntimeError('Hardware thermal thresholds changed')
    r.thermal.check(sample, baseline, initial=initial)


def prepare():
    frozen, inputs, baseline, accepted, review, manifest = r.review.review()
    prior_plan = json.loads((r.RUN/'plan.json').read_text()); old.gate.verify_hashes(prior_plan['input_sha256'])
    state = json.loads((r.RUN/'status.json').read_text()); thermal = json.loads((r.RUN/'thermal-status.json').read_text())
    budget = json.loads((r.RUN/'budget.json').read_text()); exited = json.loads((r.RUN/'service-exit.json').read_text())
    if (state['status'] != 'NEEDS_ATTENTION' or state['guard_error'] != 'Thermal guard: CPU sensor 85.0 C reached stop threshold'
            or thermal['status'] != 'NEEDS_ATTENTION' or thermal['peak_temperature_c'] != 85
            or state['completed_continuation_batches'] != 0 or (r.RUN/'completion.json').exists()
            or json.loads((r.RUN/'results.json').read_text()) != accepted
            or [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['exit-code', 'exited', '1']):
        raise ValueError('Unexpected prior thermal stop or accepted cohort')
    elapsed = (datetime.fromisoformat(exited['ended_at'])-datetime.fromisoformat(thermal['started_at'])).total_seconds()
    if (budget['used_seconds'] < prior_plan['inherited_used_seconds']+elapsed-.001 or budget['limit_seconds'] != 86400
            or budget['used_seconds']+BUDGET+prior_plan['forecast']['planning_hours']*3600+10890 > 86400):
        raise ValueError('Inherited production/diagnostic budget changed or insufficient')
    paths = {Path(__file__), ROOT/'scripts/thermal_95_gate.sh'}
    paths.update(r.RUN/n for n in ('plan.json', 'status.json', 'results.json', 'budget.json', 'thermal-status.json',
                                  'thermal-samples.jsonl', 'service-exit.json', 'logs/campaign.log'))
    stopped = list((r.RUN/'cases').glob('*/result.json'))
    if len(stopped) != 15:
        raise ValueError('Expected15 preserved interrupted attempts')
    for path in stopped:
        row = json.loads(path.read_text())
        if row['status'] != 'STOPPED' or not Path(row['attempt']).is_relative_to(r.RUN/'cases'/row['name']):
            raise ValueError('Unexpected interrupted attempt')
        old.gate.verify_hashes(row['artifact_sha256']); paths.add(path)
        paths.update(Path(x) for x in row['artifact_sha256'])
    selected = sorted((row for row in accepted if row['energy_kev'] == 1000 and row['repeat'] == 0
        and row['ion'] in ('C', 'F') and (row['pressure_atm'] in (2.5, 3.) or (row['pressure_atm'] == 2. and row['ion'] == 'C'))),
        key=lambda row: row['wall_seconds'], reverse=True)
    indexed = {c['name']: c for c in frozen['cases']}
    cases = [dict(indexed[row['name']], diagnostic=True, replay_reference=row['attempt']) for row in selected]
    if len(cases) != 15 or len(baseline) != 4536 or len(accepted) != 251:
        raise ValueError('Diagnostic/reference cohort changed')
    manifest = {**manifest, **prior_plan['input_sha256'], **{str(x): p.history.actual_sha256(x) for x in sorted(paths)}}
    plan = dict(stage='C1g', cases=cases, input_sha256=manifest, workers=15, diagnostic_histories=750,
                new_independent_production_histories=0, inherited_used_seconds=budget['used_seconds'],
                budget_seconds=BUDGET, expected_compute_minutes=[10, 20],
                reference_max_batch_seconds=max(row['wall_seconds'] for row in selected),
                thermal_policy=dict(user_ceiling_c=100, stop_at_c=95, critical_headroom_c=5, start_below_c=75,
                                    poll_seconds=POLL, stop_on_new_throttling=True, stop_on_telemetry_loss=True,
                                    stop_action='SIGSTOP then SIGKILL only other processes in this exact service cgroup; preserve partial runtimes',
                                    overshoot_guaranteed_impossible=False),
                thread_environment=THREAD_ENV, startup_stagger_seconds=1,
                user_amendment='User explicitly accepts temperatures above85C at15cores, requires not exceeding100C. '
                               'Use95C trip with5C headroom andfaster sampling; softwarecannot guaranteezero transientovershoot. '
                               'Replaces unlaunched8-workerC1g design,not retrospective changes tooldruns.',
                hardware_reference=dict(cpu='Intel Core Ultra 7 265', manufacturer_max_c=105, source=INTEL_URL),
                review=dict(previous_thermal_stop_seconds=elapsed, inherited_used_seconds=budget['used_seconds'],
                            accepted_batches=251, pending_batches=1009, root_cause_proven=False),
                gates='15exact1MeV50-ion diagnosticreplays under15-core/24-32GiB/zero-scan-swap limits. '
                      'Raw/input/actualparameter/composition/density/containment/normalcompletion/noreset/source checks; '
                      'fourkeyoutputs byte-identical.15activeworkersobserved;complete thermaltrace andresourceaudit. '
                      'Stopforcallbackbeforeproduction;noautomaticretry oradditionalphysics histories.')
    if (RUN/'plan.json').exists() and read('plan.json') != plan:
        raise ValueError('Frozen C1g95 plan changed')
    if not (RUN/'plan.json').exists():
        write_json(RUN/'plan.json', plan)
    return plan, inputs


def kill_owned_workers(group):
    """Remove heat promptly, without granting a hot workload a30-second drain."""
    if group.name != UNIT:
        raise RuntimeError('Refusing to signal processes outside the exact gate cgroup')
    relative = str(group).removeprefix('/sys/fs/cgroup')
    targeted = []
    for sig in (signal.SIGSTOP, signal.SIGKILL):
        for text_pid in (group/'cgroup.procs').read_text().split():
            pid = int(text_pid)
            if pid == os.getpid():
                continue
            try:
                memberships = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                if not any(line == '0::'+relative for line in memberships):
                    continue
                os.kill(pid, sig)
                if sig == signal.SIGSTOP:
                    targeted.append(pid)
            except (ProcessLookupError, FileNotFoundError):
                pass
    return targeted


def audit_replay(case, row, contents):
    saved = g.RUN; g.RUN = RUN
    try:
        g.audit_replay(case, row, contents)
    finally:
        g.RUN = saved


def run(deadline):
    state = dict(status='RUNNING', stage='C1g', phase='cold_review', started_at=datetime.now().astimezone().isoformat())
    active, rows = {}, []
    def update():
        state.update(updated_at=datetime.now().astimezone().isoformat(), completed_replays=len(rows), resources=resource_sample())
        write_json(RUN/'status.json', state)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><meta http-equiv="refresh" content="15">'
                    '<h1>C1g:15-core thermal validation</h1><p>95C stop,100C user ceiling. '
                    'All750histories are diagnostic; production held until review.</p>'+table([state], list(state)))
    try:
        update(); plan, inputs = prepare()
        if memory_info()['MemAvailable'] < 16*GIB or deadline-time.monotonic() < 1200:
            raise RuntimeError('Insufficient RAM or fixed-replay time reserve')
        config = scan.load_config(b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, WORKERS, context)
        state['phase'] = 'staggered_worker_startup'; update()
        for case, runtime in zip(plan['cases'], runtimes):
            if any(process.poll() not in (None, 0) for process in active):
                raise RuntimeError('Worker failure during startup; hold remaining launches')
            with (RUN/'logs'/(case['name']+'.log')).open('xb') as stream:
                process = subprocess.Popen([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'child',
                    '--case', case['name'], '--runtime', str(runtime), '--deadline', str(deadline)],
                    stdout=stream, stderr=subprocess.STDOUT)
            active[process] = case
            state['maximum_active_workers'] = max(state.get('maximum_active_workers', 0), sum(x.poll() is None for x in active))
            update(); time.sleep(1)
        state['phase'] = 'sustained_1MeV_exact_replays'; update()
        while active:
            if time.monotonic() >= deadline-30:
                raise TimeoutError('Replay gate deadline reached')
            for process, case in list(active.items()):
                code = process.poll()
                if code is None:
                    continue
                if code != 0:
                    raise RuntimeError(f'Isolated replay {case["name"]} exited{code}')
                row = read('cases/'+case['name']+'/result.json')
                with old.gate.retry.parser_context():
                    audit_replay(case, row, inputs[case['name']])
                rows.append(row); del active[process]
                write_json(RUN/'results.json', rows)
            update(); time.sleep(1)
        old.gate.verify_hashes(plan['input_sha256'])
        if len(rows) != 15 or state['maximum_active_workers'] != 15:
            raise ValueError('Incomplete cohort or full15-worker load not observed')
        group = Path(resource_sample()['cgroup'])
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise ValueError('OOM observed')
        state.update(status='REVIEW_REQUIRED', phase='Replay gate passed; review thermal trace before production',
                     source_unchanged=True, exact_replays_passed=15, diagnostic_histories=750,
                     new_independent_production_histories=0, root_cause_proven=False)
        update(); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='NEEDS_ATTENTION', error=str(exc)); update(); return 1


def guard():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15
            or (group/'memory.max').read_text().strip() != str(32*GIB)
            or (group/'memory.swap.max').read_text().strip() != '0'
            or any(os.environ.get(k) != v for k, v in THREAD_ENV.items())):
        raise RuntimeError('Use bounded C1g95 launcher with single-thread numerical libraries')
    if (RUN/'thermal-status.json').exists() or (RUN/'status.json').exists():
        raise RuntimeError('Already attempted; no automatic retry')
    inherited = read('plan.json')['inherited_used_seconds']
    start = time.monotonic(); deadline = start+min(BUDGET, 86400-inherited)
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), samples=0,
                 inherited_used_seconds=inherited, stop_threshold_c=95, user_ceiling_c=100)
    process = None
    def stop(*_):
        raise InterruptedError('Gate stop requested')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    try:
        baseline = thermal_snapshot(); thermal_check(baseline, baseline, initial=True)
        state.update(baseline=baseline, peak_temperature_c=max(baseline['temperatures_c'].values()))
        write_json(RUN/'thermal-status.json', state)
        write_json(RUN/'session.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                                          cpu_affinity=sorted(os.sched_getaffinity(0)), thread_environment=THREAD_ENV))
        process = subprocess.Popen([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'run', '--deadline', str(deadline)])
        with (RUN/'thermal-samples.jsonl').open('x') as stream:
            while True:
                current = thermal_snapshot()
                state.update(samples=state['samples']+1, latest=current,
                             peak_temperature_c=max(state['peak_temperature_c'], max(current['temperatures_c'].values())))
                # Check before disk IO: stop hot workers first if the threshold is reached.
                thermal_check(current, baseline)
                if time.monotonic() >= deadline:
                    raise TimeoutError('Bounded thermal gate deadline reached')
                stream.write(json.dumps(current)+'\n'); stream.flush()
                write_json(RUN/'thermal-status.json', state)
                code = process.poll()
                if code is not None:
                    if code != 0 or read('status.json') != read('completion.json') or read('status.json')['status'] != 'REVIEW_REQUIRED':
                        raise RuntimeError(f'Controller exited{code} without valid completion')
                    break
                time.sleep(POLL)
        state.update(status='COMPLETE', child_returncode=0); return 0
    except Exception as exc:
        state['stopped_worker_pids'] = kill_owned_workers(group)
        if process is not None:
            process.wait(timeout=10)
        state.update(status='NEEDS_ATTENTION', error=str(exc))
        status = read('status.json') if (RUN/'status.json').exists() else {}
        status.update(status='NEEDS_ATTENTION', guard_error=str(exc))
        write_json(RUN/'status.json', status)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>C1g needs attention</h1>'+table([status], list(status)))
        print(str(exc), flush=True); return 1
    finally:
        state.update(ended_at=datetime.now().astimezone().isoformat(), wall_seconds=time.monotonic()-start)
        write_json(RUN/'thermal-status.json', state)
        write_json(RUN/'budget.json', dict(used_seconds=inherited+time.monotonic()-start, limit_seconds=86400))


def main():
    faulthandler.enable(all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'guard', 'run', 'child', 'status', 'finalize'))
    parser.add_argument('--case'); parser.add_argument('--runtime', type=Path); parser.add_argument('--deadline', type=float)
    args = parser.parse_args()
    if args.command == 'finalize':
        r.RUN = RUN; r.finalize(); return 0
    if args.command == 'status':
        r.RUN, r.UNIT = RUN, UNIT
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        for name in ('status.json', 'thermal-status.json', 'budget.json'):
            if (RUN/name).exists():
                print(json.dumps({k:v for k,v in read(name).items() if k not in ('baseline', 'latest')}, indent=2))
        print('Report:', RUN/'index.html'); return 0
    if args.command == 'child':
        g.RUN = RUN; return g.child(args.case, args.runtime, args.deadline)
    if args.command == 'run':
        return run(args.deadline)
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'guard':
            return guard()
        plan, _ = prepare()
        print(json.dumps({k: plan[k] for k in ('stage', 'workers', 'diagnostic_histories', 'inherited_used_seconds', 'thermal_policy')}, indent=2))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
