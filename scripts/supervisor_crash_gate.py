#!/usr/bin/env python3
"""C1f: bounded crash-containment validation; no automatic production release."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import faulthandler
import fcntl
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
from scripts import mixture_pressure_65 as old
from scripts.preliminary_report import atomic_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

p, b, scan, gate = old.p, old.b, old.scan, old.gate
RUN = ROOT/'runs/supervisor_crash_gate'
UNIT = 'srim-supervisor-crash-gate.service'
BUDGET = 900
WORKERS = 15


def crash_evidence():
    path = RUN/'crash-journal.json'
    if not path.exists():
        raw = subprocess.check_output(['journalctl', '--user', '-u', old.UNIT, '--since',
            '2026-10-07 03:18:25', '--until', '2026-10-07 03:18:40', '-o', 'json', '--no-pager'], text=True)
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        write_json(path, records)
    records = json.loads(path.read_text())
    exits = [r for r in records if 'Main process exited, code=dumped, status=11/SEGV' in r.get('MESSAGE', '')]
    if len(exits) != 1:
        raise ValueError('Expected exactly one recorded supervisor SIGSEGV')
    return records, exits[0]


def review():
    frozen, inputs, baseline = old.prepare(old.RUN)
    gate.verify_hashes(frozen['input_sha256'])
    records, exited = crash_evidence()
    state = json.loads((old.RUN/'status.json').read_text())
    rows = json.loads((old.RUN/'results.json').read_text())
    if ((old.RUN/'completion.json').exists() or state['status'] != 'RUNNING'
            or state['phase'] != 'pressure_production' or not state['low_pressure_gate_passed']
            or not state['high_pressure_gate_passed'] or len(rows) != 251):
        raise ValueError('Unexpected original interrupted state')
    indexed = {c['name']: c for c in frozen['cases']}
    accepted = {r['name'] for r in rows}
    if len(accepted) != 251 or len(baseline) != 4536:
        raise ValueError('Duplicate or incompatible production baseline')
    paths = {old.RUN/n for n in ('plan.json', 'status.json', 'results.json', 'budget.json', 'logs/campaign.log')}
    with gate.retry.parser_context():
        for row in rows:
            path = old.RUN/'cases'/row['name']/'result.json'; paths.add(path)
            if row != json.loads(path.read_text()) or row['diagnostic']:
                raise ValueError('Case/index mismatch or diagnostic pooled')
            old.audit_case(indexed[row['name']], row, inputs[row['name']])
            paths.update(Path(x) for x in row['artifact_sha256'])
    unfinished = sorted(d for d in (old.RUN/'cases').iterdir() if not (d/'result.json').exists())
    if len(unfinished) != 15 or any(d.name not in indexed for d in unfinished):
        raise ValueError('Unexpected interrupted cohort')
    if {d.name for d in (old.RUN/'cases').iterdir()} != accepted | {d.name for d in unfinished}:
        raise ValueError('Unindexed case records')
    for directory in unfinished:
        paths.update(x for x in directory.rglob('*') if x.is_file())
    # Preserve remaining worker evidence by fingerprint; never reuse or clean it.
    for runtime in (old.RUN/'runtime').glob('worker-*'):
        paths.update(scan.collect_runtime_outputs(runtime).values())
        paths.update(x for pattern in ('TRIM.IN', 'TRIM.dat', 'TRIM.DAT', '.*.json', '*.log', '*screen.png', '*window-tree.txt')
                     for x in runtime.glob(pattern) if x.is_file())
    points = b.p.point_summary(baseline+rows)
    pending = [c for c in frozen['cases'] if c['name'] not in accepted]
    budget = json.loads((old.RUN/'budget.json').read_text())
    # Include the last heartbeat gap and conservative 90-second termination reserve.
    epoch = int(exited['__REALTIME_TIMESTAMP'])/1e6
    inherited = max(budget['used_seconds'], math.ceil(epoch-datetime.fromisoformat(state['started_at']).timestamp()))+90
    forecast = old.forecast(pending)
    if len(pending) != 1009 or len({r['seed'] for r in baseline+rows}) != 4787:
        raise ValueError('Remaining cohort or independent seeds changed')
    if inherited/3600+BUDGET/3600+forecast['planning_hours']+3.025 > 24:
        raise ValueError('Diagnostic plus continuation does not fit inherited budget')
    summary = dict(status='AUDITED_SIGSEGV_CAUSE_UNPROVEN', accepted_batches=251, interrupted_batches=15,
                   unlaunched_batches=994, remaining_batches=1009, independent_batches=4787,
                   complete_points=sum(x['complete'] for x in points), partial_points=sum(not x['complete'] for x in points),
                   precision_flags=sum(x['complete'] and not x['precision_met'] for x in points),
                   inherited_used_seconds=inherited, original_last_heartbeat=state['updated_at'],
                   crash_message=exited['MESSAGE'], remaining_forecast=forecast,
                   resources_at_last_heartbeat=state['resources'], root_cause_proven=False,
                   decision='Preserve251 raw-audited production batches and15 interrupted attempts. Original RUNNING status is stale. '
                            'Supervisor SIGSEGV is established; no accessible core/native backtrace, no claim of a proven thread defect. '
                            'Validate subprocess fault containment, bounded threaded raw audits and15 exact diagnostic replays. '
                            'Diagnostic histories are excluded; manual callback before a versioned1009-batch continuation. '
                            'Carry original elapsed time,90s conservative termination reserve and diagnostic wall time into original24h cap.')
    return frozen, inputs, rows, paths, summary


def prepare():
    frozen, inputs, rows, paths, summary = review()
    indexed = {c['name']: c for c in frozen['cases']}
    # One per family/recoil at0.5atm, plus C/F at3atm: fixed15 one-keV controls.
    selected = sorted((r for r in rows if r['energy_kev'] == 1 and r['repeat'] == 0
                       and (r['pressure_atm'] == .5 or (r['pressure_atm'] == 3 and r['ion'] in ('C', 'F')))),
                      key=lambda r: r['name'])
    cases = [dict(indexed[r['name']], diagnostic=True, replay_reference=r['attempt']) for r in selected]
    if len(cases) != 15 or len({c['name'] for c in cases}) != 15:
        raise ValueError('Diagnostic replay selection changed')
    paths.update((Path(__file__), ROOT/'scripts/supervisor_crash_gate.sh', RUN/'crash-journal.json',
                  Path(sys.executable).resolve()))
    manifest = {**frozen['input_sha256'], **{str(x): p.history.actual_sha256(x) for x in sorted(paths)}}
    plan = dict(stage='C1f', cases=cases, accepted_names=sorted(r['name'] for r in rows), review=summary,
                input_sha256=manifest, diagnostic_histories=750, new_independent_production_histories=0,
                budget_seconds=BUDGET, workers=15, memory_max_gib=32, swap_max_gib=0,
                threaded_audit_passes=3, fault_injection='SIGSEGV in dedicated child with faulthandler; parent must survive',
                expected_compute_minutes=[3, 8], manual_review_before_production=True,
                gates='251 complete raw audits,3x251 concurrent re-audits,isolated child SIGSEGV observed withoutparent crash, '
                      '15 exact50-ion replays using original inputs/seeds and validated noreset/normalcompletion/physics gates. '
                      'All four key raw outputs byte-identical; no diagnostic promotion. Stop aftergate;noautomatic retries.')
    target = RUN/'plan.json'
    if target.exists() and json.loads(target.read_text()) != plan:
        raise ValueError('Frozen crash gate changed')
    if not target.exists():
        write_json(target, plan)
    write_json(RUN/'previous_review.json', summary)
    return plan, inputs, rows


def audit_replay(case, row, contents):
    old.original_audit(case, row, contents)
    if (not case['diagnostic'] or not row.get('replay_passed') or
            not Path(row['attempt']).is_relative_to(RUN/'cases') or
            row['display_startup'].get('reset_on_last_client') is not False or
            row['display_startup'].get('helper') != str(Path(gate.fixed_display.__file__))):
        raise ValueError('Isolated replay integrity gate failed')


def child(case_name, runtime, deadline):
    plan = json.loads((RUN/'plan.json').read_text())
    case = next(c for c in plan['cases'] if c['name'] == case_name)
    reference = Path(case['replay_reference'])
    contents = (reference/'TRIM.IN').read_bytes(), (reference/'TRIM.DAT').read_bytes()
    p.RUN = b.RUN = b.base.RUN = RUN
    p.audit_case, p.check_composition, scan.launch_command = audit_replay, gate.retry.check_composition, gate.launch
    def stop(sig, frame):
        scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    context = scan.resolve_execution_context(scan.load_config(b.base.d.CONFIG))
    row = p.run_case(case, contents, runtime, scan.runtime_execution_context(context, RUN, runtime), deadline)
    return 0 if row['status'] == 'COMPLETE' else 1


def spawn_checked(command, log, timeout):
    """Single-threaded controller tracks dedicated processes; never hide nonzero exits."""
    with log.open('wb') as output:
        process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                   env=dict(os.environ, PYTHONFAULTHANDLER='1'))
    return process, time.monotonic()+timeout


def stress():
    plan = json.loads((RUN/'plan.json').read_text())
    original = json.loads((old.RUN/'plan.json').read_text())
    cases = {c['name']: c for c in original['cases']}
    rows = json.loads((old.RUN/'results.json').read_text())
    def check(row):
        directory = Path(row['attempt'])
        old.audit_case(cases[row['name']], row, ((directory/'TRIM.IN').read_bytes(), (directory/'TRIM.DAT').read_bytes()))
        return row['name']
    with gate.retry.parser_context(), ThreadPoolExecutor(max_workers=15) as pool:
        for _ in range(plan['threaded_audit_passes']):
            if set(pool.map(check, rows)) != set(plan['accepted_names']):
                raise ValueError('Incomplete threaded re-audit')
    write_json(RUN/'stress.json', dict(status='COMPLETE', passes=3, batch_audits=753, sigsegv_reproduced=False))
    return 0


def run():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15 or
            (group/'memory.max').read_text().strip() != str(32*GIB) or (group/'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bounded crash-gate launcher')
    if (RUN/'status.json').exists() or memory_info()['MemAvailable'] < 16*GIB:
        raise RuntimeError('Already attempted or insufficient RAM')
    start = time.monotonic(); deadline = start+BUDGET; active = []
    state = dict(status='RUNNING', stage='C1f', phase='auditing_saved_production', started_at=datetime.now().astimezone().isoformat())
    def stop(sig, frame):
        raise InterruptedError('Gate interrupted; production held')
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    def update():
        state.update(updated_at=datetime.now().astimezone().isoformat(), wall_seconds=time.monotonic()-start, resources=resource_sample())
        write_json(RUN/'status.json', state)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><meta http-equiv="refresh" content="15">'
                    '<h1>C1f: supervisor crash validation</h1><p>Production held. Diagnostic replays add no independent production samples. '
                    '<a href="previous_review.json">Saved-production review</a></p>'+table([state], list(state)))
    def wait_children():
        while active:
            if time.monotonic() >= deadline:
                raise TimeoutError('Gate budget exceeded')
            for process, end in list(active):
                code = process.poll()
                if code is not None:
                    active.remove((process, end))
                    if code != 0:
                        raise RuntimeError(f'Isolated child {process.pid} failed with return code {code}; inspect logs')
                elif time.monotonic() > end:
                    raise TimeoutError('Isolated child timeout')
            update()
            if active:
                time.sleep(1)
    try:
        update(); plan, inputs, _ = prepare()
        write_json(RUN/f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        state['phase'] = 'controlled_child_fault'; update()
        injection = [sys.executable, '-X', 'faulthandler', '-c',
                     'import os,signal,resource; resource.setrlimit(resource.RLIMIT_CORE,(0,0)); os.kill(os.getpid(),signal.SIGSEGV)']
        process, _ = spawn_checked(injection, RUN/'fault-injection.log', 10)
        active.append((process, time.monotonic()+10))
        code = process.wait(timeout=10); active.clear()
        if code != -signal.SIGSEGV or 'Fatal Python error: Segmentation fault' not in (RUN/'fault-injection.log').read_text():
            raise ValueError('Child fault not detected or faulthandler evidence missing')
        write_json(RUN/'fault-injection.json', dict(child_returncode=code, parent_survived=True, intentional=True))
        state['phase'] = 'threaded_saved_output_stress'; update()
        active.append(spawn_checked([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'stress'], RUN/'stress.log', 240))
        wait_children()
        if deadline-time.monotonic() < 360:
            raise TimeoutError('Insufficient diagnostic replay/drain reserve')
        config = scan.load_config(b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, 15, context)
        state['phase'] = 'isolated_exact_replays'; update()
        for case, runtime in zip(plan['cases'], runtimes):
            active.append(spawn_checked([sys.executable, '-X', 'faulthandler', str(Path(__file__)), 'child',
                '--case', case['name'], '--runtime', str(runtime), '--deadline', str(deadline)], RUN/(case['name']+'.log'), 300))
        wait_children()
        rows = []
        with gate.retry.parser_context():
            for case in plan['cases']:
                row = json.loads((RUN/'cases'/case['name']/'result.json').read_text())
                audit_replay(case, row, inputs[case['name']]); rows.append(row)
        gate.verify_hashes(plan['input_sha256'])
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise ValueError('OOM observed')
        write_json(RUN/'results.json', rows)
        state.update(status='REVIEW_REQUIRED', phase='Gate passed; review before versioned G5-65 continuation',
                     source_unchanged=True, exact_replays_passed=15, diagnostic_histories=750,
                     new_independent_production_histories=0, threaded_batch_audits=753,
                     root_cause_proven=False, spontaneous_sigsegv_reproduced=False,
                     inherited_used_seconds=plan['review']['inherited_used_seconds'])
        update(); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        for process, _ in active:
            if process.poll() is None:
                process.terminate()
        for process, _ in active:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        state.update(status='NEEDS_ATTENTION', error=str(exc)); update(); return 1


def main():
    faulthandler.enable(all_threads=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status', 'stress', 'child', 'finalize'))
    parser.add_argument('--case'); parser.add_argument('--runtime', type=Path); parser.add_argument('--deadline', type=float)
    args = parser.parse_args()
    if args.command == 'finalize':
        ended = datetime.now().astimezone()
        evidence = dict(ended_at=ended.isoformat(), service_result=os.environ.get('SERVICE_RESULT'),
                        exit_code=os.environ.get('EXIT_CODE'), exit_status=os.environ.get('EXIT_STATUS'))
        write_json(RUN/'service-exit.json', evidence)
        if (RUN/'status.json').exists():
            state = json.loads((RUN/'status.json').read_text())
            if state['status'] == 'RUNNING':
                state.update(status='NEEDS_ATTENTION', phase='Service exited before terminal status', service_exit=evidence,
                             wall_seconds=(ended-datetime.fromisoformat(state['started_at'])).total_seconds())
                write_json(RUN/'status.json', state)
                atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>C1f: needs attention</h1>'+
                            '<p>Service exited; production remains held.</p>'+table([state], list(state)))
        return 0
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    if args.command == 'child':
        return child(args.case, args.runtime, args.deadline)
    if args.command == 'stress':
        return stress()
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(); print(json.dumps(plan['review'], indent=2)); return 0
        return run()


if __name__ == '__main__':
    raise SystemExit(main())
