#!/usr/bin/env python3
"""C1e: diagnose last-client resets and gate a versioned noreset launcher."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixtures_1atm_retry as retry
from scripts import private_xvfb as old_display
from scripts import private_xvfb_noreset as fixed_display
from scripts.preliminary_report import atomic_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

p, scan = retry.p, retry.p.scan
OLD_RUN = retry.RUN
RUN = ROOT/'runs/xvfb_reset_gate'
UNIT = 'srim-xvfb-reset-gate.service'
BUDGET = 900
FAILED = 'Xe_C_e300_n90_p1_b2'
REFERENCE = 'Kr_F_e1_n75_p1_b0'


def verify_hashes(manifest):
    for path, digest in manifest.items():
        if p.history.actual_sha256(path) != digest:
            raise ValueError('Frozen evidence changed: '+path)


def review():
    frozen, inputs, baseline = retry.prepare(OLD_RUN)
    verify_hashes(frozen['input_sha256'])
    state = json.loads((OLD_RUN/'status.json').read_text())
    rows = json.loads((OLD_RUN/'results.json').read_text())
    if state['status'] != 'NEEDS_ATTENTION' or Counter(r['status'] for r in rows) != {'COMPLETE': 401, 'FAILED': 1, 'STOPPED': 14}:
        raise ValueError('Unexpected M3R stop state')
    if len({r['name'] for r in rows}) != 416:
        raise ValueError('Duplicate M3R batch')
    cases = {c['name']: c for c in frozen['cases']}; accepted = []
    paths = {OLD_RUN/name for name in ('plan.json', 'status.json', 'results.json', 'budget.json')}
    with retry.parser_context():
        for row in rows:
            path = OLD_RUN/'cases'/row['name']/'result.json'; paths.add(path)
            if row != json.loads(path.read_text()) or any(row[k] != v for k, v in cases[row['name']].items()):
                raise ValueError('M3R case/index changed')
            verify_hashes(row['artifact_sha256']); paths.update(Path(x) for x in row['artifact_sha256'])
            directory = Path(row['attempt'])
            if ((directory/'TRIM.IN').read_bytes(), (directory/'TRIM.DAT').read_bytes()) != inputs[row['name']]:
                raise ValueError('Executed input changed')
            if row['status'] == 'COMPLETE':
                p.audit_case(cases[row['name']], row, inputs[row['name']]); accepted.append(row)
    failure = next(r for r in rows if r['status'] == 'FAILED')
    if (failure['name'] != FAILED or 'unable to open display' not in failure['error']
            or 'line 100, in monitored_trim_run' not in failure['error']
            or failure['display_startup']['status'] != 'READY'):
        raise ValueError('Not the reviewed pre-TRIM display-access failure')
    if (Path(failure['attempt'])/'.trim-progress.json').exists():
        raise ValueError('Failed batch unexpectedly launched TRIM')
    if len({r['seed'] for r in baseline+accepted}) != 1445:
        raise ValueError('Independent seed accounting changed')
    points = p.b.p.point_summary(baseline+accepted)
    pending = [c for c in frozen['cases'] if c['name'] not in {r['name'] for r in accepted}]
    spent = json.loads((OLD_RUN/'budget.json').read_text())['used_seconds']
    summary = dict(status='AUDITED_PRE_TRIM_DISPLAY_ACCESS_FAILURE', accepted_m3_batches=401,
                   combined_independent_batches=1445, combined_histories=72250,
                   combined_complete_points=sum(x['complete'] for x in points), partial_points=45,
                   precision_flags=sum(x['complete'] and not x['precision_met'] for x in points),
                   failed_cases=1, interrupted_cases=14, unlaunched_cases=844, remaining_m3_batches=859,
                   composition_canaries_passed=45, inherited_used_seconds=spent,
                   remaining_frozen_forecast=p.forecast(pending), resources=state['resources'],
                   diagnosis='Xvfb reported READY; a subsequent completion-watchdog connection failed before TRIM launch. '
                             'Last-client reset is a hypothesis tested by repeated disconnect/reconnect controls, not a proven physics fault.',
                   decision='Preserve all frozen evidence and 401 accepted independent batches. Run display-only controls and two '
                            'diagnostic replays with -noreset. No watchdog relaxation, output promotion, new production seed or automatic production. '
                            'Charge gate wall time plus inherited time against the original M3 24-hour budget on continuation.')
    return frozen, inputs, accepted, paths, summary


def prepare(run):
    frozen, inputs, accepted, paths, summary = review()
    paths.update(Path(x) for x in frozen['input_sha256'])
    paths.update((Path(__file__), ROOT/'scripts/xvfb_reset_gate.sh', Path(fixed_display.__file__),
                  Path(old_display.__file__), Path('/usr/bin/xwininfo'), Path('/usr/bin/xauth'), Path('/usr/bin/Xvfb')))
    cases = {c['name']: c for c in frozen['cases']}
    reference = next(r for r in accepted if r['name'] == REFERENCE)
    replay = dict(cases[REFERENCE], diagnostic=True, replay_reference=reference['attempt'])
    failed = dict(cases[FAILED], diagnostic=True)
    plan = dict(stage='C1e', review=summary, cases=[replay, failed],
                control_waves=4, fixed_waves=4, displays_per_wave=15, reconnects_per_display=20,
                diagnostic_histories=100, new_independent_production_histories=0,
                workers_and_cores_max=15, memory_max_gib=8, swap_max_gib=0,
                budget_seconds=BUDGET, expected_compute_minutes=[3, 8],
                inherited_used_seconds=summary['inherited_used_seconds'],
                input_sha256={str(x): p.history.actual_sha256(x) for x in sorted(paths)},
                gates='Control failures are recorded, not suppressed or assumed. All 60 noreset displays must pass 20 '
                      'reconnections, unique simultaneous allocation, authentication rejection and cleanup. '
                      'Exact completed-reference replay must reproduce IONIZ/RANGE/RANGE_3D/TDATA bytes. '
                      'Replay failed exact input/seed with all original completion/input/binding/composition/containment gates. '
                      'Both are diagnostic only, not new independent samples. Source/raw hashes checked before and after. '
                      'Manual callback before a new versioned M3 continuation; P4 held.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen reset gate changed')
    if not path.exists():
        write_json(path, plan)
    write_json(run/'previous_review.json', summary)
    return plan, inputs


def display_checks(module, label, update):
    rows = []
    for wave in range(4):
        barrier = threading.Barrier(15, timeout=30)
        def check(worker):
            directory = RUN/label/f'wave-{wave}'/f'worker-{worker:03d}'
            directory.mkdir(parents=True, exist_ok=False)
            server = None; row = dict(wave=wave, worker=worker, status='FAILED', reconnects=0)
            try:
                with module.private_display(directory) as (env, server, metadata):
                    row.update(metadata); row['status'] = 'TESTING'; barrier.wait(); row['synchronized'] = True
                    for _ in range(20):
                        subprocess.run(['xwininfo', '-root', '-tree'], env=env, capture_output=True, check=True, timeout=5)
                        row['reconnects'] += 1
                        time.sleep(.01)
                    empty = directory/'empty-authority'; empty.touch(mode=0o600)
                    denied = subprocess.run(['xwininfo', '-root'], env=dict(env, XAUTHORITY=str(empty)), capture_output=True, timeout=5)
                    if denied.returncode == 0:
                        raise ValueError('Unauthenticated client accepted')
                    row['unauthorized_client_rejected'] = True
                    barrier.wait(); row['status'] = 'COMPLETE'
            except Exception as exc:
                barrier.abort()
                row.update(status='FAILED', error=str(exc), stderr=str(getattr(exc, 'stderr', ''))[-2000:])
            row['server_reaped'] = server is None or server.poll() is not None
            if not row['server_reaped']:
                row.update(status='FAILED', error='Display cleanup failed')
            write_json(directory/'result.json', row)
            return row
        with ThreadPoolExecutor(max_workers=15) as pool:
            batch = list(pool.map(check, range(15)))
        live_names = [r['display'] for r in batch if r.get('synchronized')]
        if len(set(live_names)) != len(live_names):
            raise ValueError('Simultaneous display allocation collision')
        rows.extend(batch); update()
    result = dict(status='COMPLETE' if all(r['status'] == 'COMPLETE' for r in rows) else 'OBSERVED_FAILURE',
                  completed=sum(r['status'] == 'COMPLETE' for r in rows), checks=rows)
    write_json(RUN/(label+'.json'), result)
    return result


def launch(command, cwd, timeout_s, *args, **kwargs):
    expected = ['xvfb-run', '-a', '-e', str(cwd/'.xvfb.log'), '-s', '-screen 0 1024x768x24']
    if list(command[:6]) != expected or list(command[6:9]) != [sys.executable, str(Path(p.b.base.__file__).resolve()), 'worker']:
        raise ValueError('Unexpected worker command; no implicit rewrite')
    rewritten = [sys.executable, str(Path(fixed_display.__file__)), '--runtime', str(cwd), '--',
                 sys.executable, str(Path(p.prior.r.worker.__file__)), *command[9:]]
    return p.b.p.prior.old_launch(rewritten, cwd, timeout_s, *args, **kwargs)


def audit_replay(case, row, inputs):
    with retry.parser_context():
        p.audit_case(case, row, inputs)
    if row['status'] != 'COMPLETE' or row['display_startup'].get('reset_on_last_client') is not False:
        raise ValueError('Replay or noreset lifecycle failed')
    if row['display_startup'].get('helper') != str(Path(fixed_display.__file__)):
        raise ValueError('Unexpected display helper')
    if case.get('replay_reference') and not row.get('replay_passed'):
        raise ValueError('Reference replay not byte-identical')


def run():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15 or
            (group/'memory.max').read_text().strip() != str(8*GIB) or (group/'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bounded reset-gate launcher')
    if (RUN/'status.json').exists():
        raise RuntimeError('Gate already attempted; review rather than overwrite')
    if memory_info()['MemAvailable'] < 12*GIB:
        raise RuntimeError('Insufficient RAM')
    started = time.monotonic()
    state = dict(status='RUNNING', phase='auditing_401_batches', started_at=datetime.now().astimezone().isoformat())
    def update():
        elapsed = time.monotonic()-started
        state.update(updated_at=datetime.now().astimezone().isoformat(), wall_seconds=elapsed, resources=resource_sample())
        write_json(RUN/'status.json', state)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><meta http-equiv="refresh" content="15">'
                    '<h1>C1e: display-reset validation; production held</h1><p>15-minute cap, 15 cores, 8 GiB RAM, zero swap. '
                    'Two diagnostic replays; no independent production samples. <a href="previous_review.json">M3R review</a></p>'
                    +table([state], list(state)))
        if elapsed > BUDGET:
            raise RuntimeError('Diagnostic budget exceeded')
    try:
        update(); plan, inputs = prepare(RUN)
        write_json(RUN/f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        state['phase'] = 'original_display_reconnect_control'; update()
        control = display_checks(old_display, 'control', update)
        state.update(control_displays_passed=control['completed'], phase='noreset_display_reconnect_gate'); update()
        fixed = display_checks(fixed_display, 'noreset', update)
        state['noreset_displays_passed'] = fixed['completed']
        if fixed['status'] != 'COMPLETE' or fixed['completed'] != 60:
            raise ValueError('Noreset display gate failed')
        config = scan.load_config(p.b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, 1, context)
        runtime = runtimes[0]; saved = p.RUN, p.b.RUN, p.b.base.RUN, p.check_composition, scan.launch_command
        try:
            p.RUN = p.b.RUN = p.b.base.RUN = RUN
            p.check_composition, scan.launch_command = retry.check_composition, launch
            for case in plan['cases']:
                state['phase'] = 'diagnostic_replay_'+case['name']; update()
                row = p.run_case(case, inputs[case['name']], runtime, scan.runtime_execution_context(context, RUN, runtime), started+BUDGET)
                audit_replay(case, row, inputs[case['name']])
                state.setdefault('replays_passed', []).append(case['name']); update()
        finally:
            p.RUN, p.b.RUN, p.b.base.RUN, p.check_composition, scan.launch_command = saved
        verify_hashes(plan['input_sha256'])
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise ValueError('OOM observed')
        state.update(status='REVIEW_REQUIRED', phase='Display gate passed; review before M3 continuation',
                     source_unchanged=True, diagnostic_histories=100, new_independent_production_histories=0,
                     inherited_used_seconds=plan['inherited_used_seconds'],
                     reset_hypothesis_reproduced=control['completed'] < 60)
        update()
        artifacts = {RUN/'control.json', RUN/'noreset.json'}
        for label in ('control', 'noreset'):
            artifacts.update(x for x in (RUN/label).rglob('*') if x.is_file())
        for case in plan['cases']:
            path = RUN/'cases'/case['name']/'result.json'; artifacts.add(path)
            artifacts.update(Path(x) for x in json.loads(path.read_text())['artifact_sha256'])
        state['artifact_sha256'] = {str(x): p.history.actual_sha256(x) for x in sorted(artifacts)}
        write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='NEEDS_ATTENTION', error=str(exc))
        # A terminal failure must remain reportable even after the budget expires.
        state.update(wall_seconds=time.monotonic()-started, resources=resource_sample())
        write_json(RUN/'status.json', state)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>C1e: needs attention; production held</h1>'
                    +table([state], list(state)))
        print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN)
            print(json.dumps({k: plan[k] for k in ('stage', 'review', 'expected_compute_minutes', 'budget_seconds')}, indent=2)); return 0
        return run()


if __name__ == '__main__':
    raise SystemExit(main())
