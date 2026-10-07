#!/usr/bin/env python3
"""Additive C1f thermal guard; frozen diagnostic/scientific sources stay unchanged."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.staged_scan import write_json
from scripts.preliminary_report import atomic_text, table

RUN = ROOT/'runs/supervisor_crash_gate'


def snapshot(hwmon=Path('/sys/class/hwmon'), cpus=Path('/sys/devices/system/cpu')):
    temperatures, limits = {}, {}
    for name in hwmon.glob('hwmon*/name'):
        if name.read_text().strip() != 'coretemp':
            continue
        for sensor in name.parent.glob('temp*_input'):
            temperatures[str(sensor)] = int(sensor.read_text())/1000
            limits[str(sensor)] = min(85., int(sensor.with_name(sensor.name.replace('_input', '_max')).read_text())/1000)
    counters = {str(p): int(p.read_text()) for p in cpus.glob('cpu[0-9]*/thermal_throttle/*_throttle_count')}
    if not temperatures or not counters:
        raise RuntimeError('CPU temperature/throttle telemetry unavailable; fail closed')
    return dict(time=datetime.now().astimezone().isoformat(), temperatures_c=temperatures,
                limits_c=limits, throttle_counts=counters)


def check(current, baseline, initial=False):
    for field in ('temperatures_c', 'throttle_counts', 'limits_c'):
        if current[field].keys() != baseline[field].keys():
            raise RuntimeError('Thermal telemetry set changed')
    if current['limits_c'] != baseline['limits_c']:
        raise RuntimeError('Thermal threshold changed')
    for path, value in current['temperatures_c'].items():
        limit = min(75., current['limits_c'][path]) if initial else current['limits_c'][path]
        if value >= limit:
            raise RuntimeError(f'Thermal guard: CPU sensor {value} C reached stop threshold')
    for path, value in current['throttle_counts'].items():
        if value != baseline['throttle_counts'][path]:
            raise RuntimeError('Thermal guard: CPU throttle counter changed')


def prepare():
    paths = [Path(__file__), ROOT/'scripts/supervisor_crash_gate_thermal.sh', RUN/'plan.json',
             ROOT/'configs/supervisor_crash_kernel_evidence.json']
    plan = dict(parent_stage='C1f', max_cpu_temperature_c=85, start_below_c=75,
                poll_seconds=.5, stop_on_any_throttle_counter_change=True,
                reason='User-provided kernel log reports repeated throttling near Python SIGSEGV; causality unproven.',
                production_held=True, require_thermal_complete_before_continuation=True,
                input_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})
    path = RUN/'thermal-plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise RuntimeError('Frozen supplemental thermal plan changed')
    if not path.exists():
        write_json(path, plan)
    return plan


def run():
    prepare()
    if (RUN/'thermal-status.json').exists():
        raise RuntimeError('Thermal-gated attempt already exists; no automatic retry')
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), samples=0)
    process = None
    def stop(sig, frame):
        raise InterruptedError('Thermal guard interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        baseline = snapshot(); check(baseline, baseline, initial=True)
        state.update(baseline=baseline, peak_temperature_c=max(baseline['temperatures_c'].values()))
        write_json(RUN/'thermal-status.json', state)
        process = subprocess.Popen([sys.executable, '-X', 'faulthandler',
                                    str(ROOT/'scripts/supervisor_crash_gate.py'), 'run'], start_new_session=True)
        with (RUN/'thermal-samples.jsonl').open('x') as stream:
            while True:
                current = snapshot()
                stream.write(json.dumps(current)+'\n'); stream.flush()
                state.update(samples=state['samples']+1, latest=current,
                             peak_temperature_c=max(state['peak_temperature_c'], max(current['temperatures_c'].values())))
                check(current, baseline)
                write_json(RUN/'thermal-status.json', state)
                code = process.poll()
                if code is not None:
                    if code:
                        raise RuntimeError(f'C1f controller exited with code {code}')
                    break
                time.sleep(.5)
        state.update(status='COMPLETE', ended_at=datetime.now().astimezone().isoformat(), child_returncode=0)
        write_json(RUN/'thermal-status.json', state)
        return 0
    except Exception as exc:
        # Stop this run's own process group only; systemd also cleans the service cgroup.
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        state.update(status='NEEDS_ATTENTION', error=str(exc), ended_at=datetime.now().astimezone().isoformat())
        write_json(RUN/'thermal-status.json', state)
        status_path = RUN/'status.json'
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        status.update(status='NEEDS_ATTENTION', phase='Thermal-guarded validation stopped', thermal_error=str(exc))
        write_json(status_path, status)
        atomic_text(RUN/'index.html', '<html><meta charset="utf-8"><h1>C1f: needs attention</h1>'+
                    '<p>Thermal-guarded validation stopped; production remains held.</p>'+table([status], list(status)))
        print(str(exc), flush=True)
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status'))
    command = parser.parse_args().command
    if command == 'prepare':
        prepare(); print('Supplemental thermal plan verified')
    elif command == 'status':
        for name in ('thermal-status.json', 'status.json'):
            path = RUN/name
            if path.exists():
                state = json.loads(path.read_text())
                print(json.dumps({k: v for k, v in state.items() if k not in ('baseline', 'latest')}, indent=2))
            else:
                print(f'{name}: not started')
    else:
        raise SystemExit(run())
