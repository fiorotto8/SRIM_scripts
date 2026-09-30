#!/usr/bin/env python3
"""C2b: rerun frozen geometry cases with validated mode-5 normal completion."""
from __future__ import annotations
import argparse
import copy
import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bulk_geometry_probe as bulk
from scripts import mode5_completion_canary as canary
from scripts import trim_dat_completion as completion
from scripts.staged_scan import write_json
from scripts.preliminary_report import atomic_text

scan, d = bulk.scan, bulk.d
RUN = ROOT / 'runs/kr_bulk_geometry_retry'
UNIT = 'srim-bulk-geometry-retry.service'
ORIGINAL_RUN = bulk.RUN
original_prepare, original_publish = bulk.prepare, bulk.publish


def checked_result(path, expected_status):
    result = json.loads(path.read_text())
    if result['status'] != expected_status: raise ValueError('Unexpected prerequisite state: ' + str(path))
    for name, digest in result['artifact_sha256'].items():
        if scan.sha256_file(Path(name)) != digest: raise ValueError('Prerequisite artifacts changed: ' + name)
    return result


def prepare(run):
    old_plan, inputs = original_prepare(ORIGINAL_RUN)
    canary.prepare()
    accepted = checked_result(canary.RUN / 'result.json', 'COMPLETE')
    failed = checked_result(ORIGINAL_RUN / 'cases/near_surface_r100_p1/result.json', 'FAILED')
    surface = checked_result(ORIGINAL_RUN / 'cases/mode2_surface_r100_p1/result.json', 'COMPLETE')
    if not accepted['source_unchanged'] or accepted['ions_confirmed'] != 1 or not accepted['parameters_match']:
        raise ValueError('Completion canary not audited')
    paths = {Path(__file__), ROOT / 'scripts/bulk_geometry_retry.sh', Path(completion.__file__),
             Path(canary.__file__), canary.RUN / 'plan.json', canary.RUN / 'result.json',
             ORIGINAL_RUN / 'plan.json', ORIGINAL_RUN / 'status.json',
             ORIGINAL_RUN / 'cases/near_surface_r100_p1/result.json',
             ORIGINAL_RUN / 'cases/mode2_surface_r100_p1/result.json'}
    for result in (accepted, failed, surface): paths.update(Path(p) for p in result['artifact_sha256'])
    plan = copy.deepcopy(old_plan)
    plan['input_sha256'].update({str(p): scan.sha256_file(p) for p in sorted(paths)})
    plan.update(stage='C2b', previous_failed_run=str(ORIGINAL_RUN), completion_canary=str(canary.RUN),
                retry_reason='Observed exact End of TRIM.DAT calculation dialog blocks normal mode-5 save/exit. '
                             'One-ion canary passed after one targeted acknowledgement. Inputs, binding physics, '
                             'seeds, case selection and all scientific gates unchanged; a new isolated run, not a resume.',
                completion_gate='For every mode-5 case require exactly one archived normal-completion acknowledgement, '
                                'normal process exit, complete final files and original 50-ion/output checks. '
                                'Errors still fail closed; no acknowledgement in mode 2. No diagnostic histories pooled.')
    path = run / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan: raise ValueError('Frozen geometry retry changed')
    if not path.exists(): write_json(path, plan)
    return plan, inputs


def run_case(case, contents, runtime, context, deadline):
    started = time.monotonic()
    directory = RUN / 'cases' / case['name']; directory.mkdir(parents=True, exist_ok=True)
    attempt = directory / f'attempt-{time.time_ns()}'; attempt.mkdir()
    result = dict(case, status='RUNNING', attempt=str(attempt))
    env = scan.pysrim_environment(context)
    try:
        scan.clear_runtime_outputs(runtime)
        # Scope cleanup to this reusable private worker's previous UI evidence.
        for pattern in ('completion-ack.json', '*-screen.png', '*-window-tree.txt', '.trim-progress.json', '.trim-dialog.png'):
            for path in runtime.glob(pattern):
                if path.is_file(): path.unlink()
        content, events = contents
        (runtime / 'TRIM.IN').write_bytes(content); (attempt / 'TRIM.IN').write_bytes(content)
        (runtime / 'TRIMAUTO').write_bytes(d.archive_data(Path(case['source']), 'TRIMAUTO'))
        (runtime / 'Data/TRIM.cfg').write_bytes(d.archive_data(Path(case['source']), 'Data/TRIM.cfg'))
        if events is not None:
            candidates = [p for p in runtime.iterdir() if p.name.lower() == 'trim.dat']
            if len(candidates) != 1: raise ValueError('Expected exactly one runtime TRIM.dat')
            candidates[0].write_bytes(events); (attempt / 'TRIM.DAT').write_bytes(events)
        timeout = min(1800, int(deadline - time.monotonic() - 45))
        if timeout <= 0: raise RuntimeError('Cumulative deadline reached')
        scan.launch_command(['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                             sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime), '--name', case['name']],
                            runtime, timeout, environment=env, process_group=True, display_name=case['name'])
        if (runtime / 'TRIM.IN').read_bytes() != content: raise ValueError('TRIM.IN changed')
        if events is not None:
            if candidates[0].read_bytes() != events: raise ValueError('TRIM.DAT changed')
            ack = json.loads((runtime / 'completion-ack.json').read_text())
            if ack['status'] != 'sent' or ack['title'] != completion.TITLE: raise ValueError('Missing completion evidence')
            for name in ('normal-completion-screen.png', 'normal-completion-window-tree.txt'):
                if not (runtime / name).is_file(): raise ValueError('Missing completion capture')
            result['normal_completion_acknowledged'] = True
        elif (runtime / 'completion-ack.json').exists(): raise ValueError('Unexpected mode-2 acknowledgement')
        result.update(bulk.analyse(case, scan.collect_runtime_outputs(runtime)), status='COMPLETE')
    except Exception as exc:
        result.update(status='STOPPED' if scan.SHUTDOWN_REQUESTED.is_set() else 'FAILED', error=str(exc))
    finally:
        for option in ('-k', '-w'):
            try: subprocess.run(['wineserver', option], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired): pass
        for name, path in scan.collect_runtime_outputs(runtime).items(): shutil.copy2(path, attempt / name)
        names = ['TRIMAUTO', '.trim-progress.json', '.trim-process.log', '.trim-dialog.png', '.xvfb.log', 'completion-ack.json']
        names += [p.name for p in runtime.glob('*-screen.png')] + [p.name for p in runtime.glob('*-window-tree.txt')]
        for name in names:
            if (runtime / name).exists(): shutil.copy2(runtime / name, attempt / name)
        shutil.copy2(runtime / 'Data/TRIM.cfg', attempt / 'TRIM.cfg')
        result['artifact_sha256'] = {str(p): scan.sha256_file(p) for p in sorted(attempt.iterdir()) if p.is_file()}
        result['wall_seconds'] = time.monotonic() - started
        write_json(directory / 'result.json', result)
    return result


def publish(state, results):
    original_publish(state, results)
    path = RUN / 'index.html'
    text = path.read_text().replace('C2a: TRIM.DAT', 'C2b: completion-aware TRIM.DAT')
    text = text.replace('<p>28 fixed', '<p>The first attempt is preserved separately. The exact normal-completion dialog '
                        'is acknowledged once; error dialogs are never dismissed. The one-ion lifecycle canary passed. '
                        'All final files and 50-ion counts remain mandatory.</p><p>28 fixed')
    atomic_text(path, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker', 'status'))
    parser.add_argument('--runtime', type=Path); parser.add_argument('--name')
    args = parser.parse_args()
    if args.command == 'worker':
        if args.name.startswith('mode2_surface_'):
            d.pysrim_runner.configure_recoil_capacity(args.runtime)
            d.pysrim_runner.monitored_trim_run(d.ArchivedInput(), args.runtime, dict(description=args.name))
        else: completion.run(args.runtime, args.name)
        return 0
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN / 'status.json').exists(): print((RUN / 'status.json').read_text())
        print('Report:', RUN / 'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN)
            print(json.dumps({k: plan[k] for k in ('stage', 'total_cases', 'diagnostic_histories', 'planning_minutes', 'budget_hours', 'retry_reason')}, indent=2)); return 0
        # Reuse the frozen supervisor logic, including two startup canaries,
        # fail-closed release, cumulative budgeting, resource checks and cache audit.
        # Module overrides are process-local; no historical source file is edited.
        bulk.RUN, bulk.UNIT = RUN, UNIT
        bulk.prepare, bulk.run_case, bulk.publish = prepare, run_case, publish
        return bulk.execute()


if __name__ == '__main__': raise SystemExit(main())
