#!/usr/bin/env python3
"""Start, inspect, stop and resume a detached native Windows CYGNO study."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import cygno_process as processes
import cygno_study as study

DRIVER = ROOT / 'scripts/cygno_study.py'
DEFAULT_CONFIG = ROOT / 'configs/cygno_hecf4_900mbar.toml'
DEFAULT_RUN = ROOT / 'runs/cygno_hecf4_900mbar_20261015'


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}


@contextmanager
def control_lock(out):
    """OS-released lock: a launcher crash cannot leave an unacquirable lease."""
    import msvcrt
    out.mkdir(parents=True, exist_ok=True)
    path = out / '.campaign-control.lock'
    # Keep this file in place: unlinking an advisory lock permits inode races.
    with path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError('Another campaign control command is active; retry after it finishes') from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _ensure_idle(out, snapshots):
    active = [p for p in snapshots if processes.campaign_command_matches(p.get('CommandLine'), DRIVER, out)]
    runtimes = processes.runtime_processes(snapshots, out)
    if active or runtimes:
        pids = sorted({p['ProcessId'] for p in active + runtimes})
        raise RuntimeError(f'Study coordinator or private runtime is still active (Windows PIDs {pids}); wait for it to finish')
    uncertain = [p['ProcessId'] for p in snapshots if (p.get('Name') or '').lower()
                 in ('trim.exe', 'srmodule.exe') and not p.get('ExecutablePath')]
    if uncertain:
        raise RuntimeError(f'Cannot establish runtime ownership for Windows PIDs {uncertain}; recovery preserves all locks')
    by_pid = {p['ProcessId']: p for p in snapshots}
    records = [(name, read_json(out / name))
               for name in ('campaign_state.json', 'launcher.json', '.launcher-windows.lease')]
    owner_path = out / '.simulation-lock/owner.json'
    if owner_path.exists():
        records.append(('.simulation-lock/owner.json', read_json(owner_path)))
    for origin, record in records:
        pid = record.get('pid', record.get('owner_pid'))
        process = by_pid.get(pid)
        if process is None:
            continue
        if not process.get('CommandLine') or not process.get('ExecutablePath'):
            raise RuntimeError(f'Cannot verify recorded Windows PID {pid}; recovery has not changed any locks')
        identity = record.get('process_identity')
        if identity and processes.same_identity(process, identity):
            raise RuntimeError(f'Recorded owner Windows PID {pid} is still alive; wait for it to exit')
        if not identity and (record.get('owner_pid') or origin == '.simulation-lock/owner.json'):
            raise RuntimeError(f'Legacy lock owner Windows PID {pid} is alive and lacks a creation-time identity')


def _recover_locked(out, snapshots):
    """Archive stale lock ownership only after all live process checks pass."""
    _ensure_idle(out, snapshots)
    lock = out / '.simulation-lock'
    lease = out / '.launcher-windows.lease'
    if lock.exists() and any(path.name != 'owner.json' for path in lock.iterdir()):
        raise RuntimeError(f'Unexpected files in {lock}; recovery preserves this directory')
    recovered = []
    for path in (lock / 'owner.json', lease):
        if path.exists():
            recovered.append({'path': path.relative_to(out).as_posix(), 'owner': read_json(path)})
    if recovered or lock.exists():
        tag = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        study.core.atomic_write_json(out / 'recovery' / f'{tag}.json',
                                     {'recovered_at': study.core.utc_now(), 'ownership': recovered,
                                      'action': 'released verified stale locks; completed batches preserved'})
        (lock / 'owner.json').unlink(missing_ok=True)
        if lock.exists():
            lock.rmdir()
        lease.unlink(missing_ok=True)
    state_path = out / 'campaign_state.json'
    state = read_json(state_path)
    if state.get('status') == 'running':
        state.update(status='interrupted_or_process_missing', recovered_at=study.core.utc_now())
        study.core.atomic_write_json(state_path, state)
    for path in (out / 'progress').glob('*.json'):
        progress = read_json(path)
        if progress.get('status') == 'running':
            progress.update(status='interrupted', recovered_at=study.core.utc_now())
            study.core.atomic_write_json(path, progress)
    return {'status': 'recovered', 'released_locks': len(recovered), 'completed_batches_preserved': True}


def recover(out):
    with control_lock(out):
        return _recover_locked(out, processes.windows_processes())


def _settings(out, config, source, workers, mode):
    previous = read_json(out / 'launcher.json')
    old_command = subprocess.list2cmdline(previous.get('command') or [])
    config = (config or previous.get('config') or processes.argument_value(old_command, '--config') or DEFAULT_CONFIG)
    source = (source or previous.get('srim_root') or processes.argument_value(old_command, '--srim-root') or ROOT)
    if workers is None:
        old_workers = previous.get('workers') or processes.argument_value(old_command, '--workers')
        workers = int(old_workers) if old_workers else None
    mode = mode or previous.get('mode') or ('extend' if 'extend' in (previous.get('command') or []) else 'campaign')
    if mode not in ('campaign', 'extend'):
        raise ValueError('mode must be campaign or extend')
    if workers is not None and workers < 1:
        raise ValueError('workers must be positive')
    config, source = Path(config).resolve(), Path(source).resolve()
    if out in (source, ROOT):
        raise ValueError('Use a separate study output directory')
    if (out / 'extension_plan.json').exists() and mode != 'extend':
        raise ValueError('This is a bounded extension; use --mode extend')
    for required in (config, DRIVER, source / 'TRIM.exe', source / 'SR Module/SRModule.exe', source / 'Data/ATOMDATA'):
        if not required.is_file():
            raise FileNotFoundError(f'Required file is unavailable: {required}')
    cfg = study.load_configuration(config)
    return config, source, workers or cfg['simulation']['workers'], mode


def start(out, config=None, source=None, workers=None, mode=None, resume=False, defer_report=False, ready_timeout=30):
    out = Path(out).resolve()
    config, source, workers, mode = _settings(out, config, source, workers, mode)
    with control_lock(out):
        snapshots = processes.windows_processes()
        if resume:
            _recover_locked(out, snapshots)
        else:
            _ensure_idle(out, snapshots)
        if (out / '.simulation-lock').exists() or (out / '.launcher-windows.lease').exists():
            raise RuntimeError('A stale study lock exists; run recover or resume to verify its owner')
        stop = out / '.stop-requested'
        if stop.exists() and not resume:
            raise RuntimeError('A stop request is present; use resume to continue completed checkpoints')
        if resume:
            stop.unlink(missing_ok=True)
        logs = out / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        tag = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:8]
        stdout_log, stderr_log = (logs / f'campaign-native-{tag}.{kind}.log' for kind in ('out', 'err'))
        command = [sys.executable, '-u', '-B', str(DRIVER), mode, '--config', str(config),
                   '--run-dir', str(out), '--srim-root', str(source), '--workers', str(workers)]
        if defer_report:
            command.append('--defer-report')
        with stdout_log.open('wb') as stdout, stderr_log.open('wb') as stderr:
            child = subprocess.Popen(command, cwd=source, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        # Keep the owned handle until exit when embedded in another Python
        # process; this daemon never keeps the short-lived launcher alive.
        threading.Thread(target=child.wait, daemon=True).start()
        record = {'pid': child.pid, 'process_identity': processes.process_identity(child.pid),
                  'platform': 'nt', 'coordinator_platform': 'nt', 'status': 'starting', 'host': socket.gethostname(),
                  'command': command, 'config': str(config), 'srim_root': str(source), 'workers': workers, 'mode': mode,
                  'log': str(stdout_log), 'stdout_log': str(stdout_log), 'stderr_log': str(stderr_log),
                  'report_deferred': defer_report, 'updated_at': study.core.utc_now()}
        study.core.atomic_write_text(out / 'campaign.pid', str(child.pid) + '\n')
        study.core.atomic_write_json(out / 'launcher.json', record)
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            state = read_json(out / 'campaign_state.json')
            owner = read_json(out / '.simulation-lock/owner.json')
            exit_code = child.poll()
            if state.get('pid') == child.pid:
                if state.get('status') == 'running' and owner.get('pid') == child.pid:
                    record['status'] = 'running'
                    break
                if exit_code is not None:
                    record.update(status=state.get('status', 'exited_before_ready'), exit_code=exit_code)
                    break
            if exit_code is not None:
                record.update(status='exited_before_ready', exit_code=exit_code)
                break
            time.sleep(.25)
        else:
            record['status'] = 'startup_unconfirmed'
        record['updated_at'] = study.core.utc_now()
        study.core.atomic_write_json(out / 'launcher.json', record)
        if record['status'] not in ('running', 'complete', 'simulations_complete_report_pending', 'stopped'):
            raise RuntimeError(f"Coordinator startup is {record['status']}; inspect PID {child.pid} and {stderr_log} before retrying")
        return record


def stop(out):
    if not out.is_dir():
        raise FileNotFoundError(f'Study directory is unavailable: {out}')
    with control_lock(out):
        (out / '.stop-requested').touch(exist_ok=True)
    return {'status': 'stop_requested', 'message': 'Workers will finish and checkpoint active batches before stopping'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start', 'status', 'stop', 'resume', 'recover'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--run-dir', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--srim-root', type=Path)
    parser.add_argument('--workers', type=int)
    parser.add_argument('--mode', choices=['campaign', 'extend'])
    parser.add_argument('--defer-report', action='store_true', help='Optionally render the native report in a later command')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Use native Windows Python: py -3.15 scripts/cygno_campaign.py ...')
    out = args.run_dir.resolve()
    if args.action == 'status':
        result = study.study_status(out)
        result.update(stop_requested=(out / '.stop-requested').exists(), simulation_lock=(out / '.simulation-lock').exists())
    elif args.action == 'stop':
        result = stop(out)
    elif args.action == 'recover':
        result = recover(out)
    else:
        result = start(out, args.config, args.srim_root, args.workers, args.mode,
                       resume=args.action == 'resume', defer_report=args.defer_report)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
