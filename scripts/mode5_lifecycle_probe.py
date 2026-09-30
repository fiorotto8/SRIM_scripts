#!/usr/bin/env python3
"""One-ion, observe-only reproduction of mode-5 termination; never dismiss dialogs."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bulk_geometry_probe as bulk
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, GIB
from scripts.preliminary_report import atomic_text, table
scan = bulk.scan
RUN = ROOT / 'runs/mode5_lifecycle_probe'
UNIT = 'srim-mode5-lifecycle.service'
FAILED = bulk.RUN / 'cases/near_surface_r100_p1/result.json'


def one_ion_inputs(original, events):
    lines = original.decode('ascii').splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('Ion:')) + 1
    fields = lines[index].split(); fields[4] = '1'; lines[index] = ' '.join(fields)
    data = events.rstrip(b'\x1a').decode('ascii').splitlines()
    if len(data) != 60 or len(data[10].split()) != 9:
        raise ValueError('Expected archived 10-header/50-event input')
    return ('\r\n'.join(lines) + '\r\n').encode('ascii'), ('\r\n'.join(data[:11]) + '\r\n').encode('ascii') + b'\x1a'


def prepare():
    old = json.loads(FAILED.read_text())
    if old['status'] != 'FAILED' or 'timed out' not in old['error']:
        raise ValueError('Expected preserved mode-5 timeout')
    for path, digest in old['artifact_sha256'].items():
        if scan.sha256_file(Path(path)) != digest: raise ValueError('Failed artifacts changed')
    origin = Path(old['attempt'])
    content, events = one_ion_inputs((origin / 'TRIM.IN').read_bytes(), (origin / 'TRIM.DAT').read_bytes())
    plan = dict(source=str(origin), source_result_sha256=scan.sha256_file(FAILED),
                code_sha256=scan.sha256_file(Path(__file__)), ions=1, budget_seconds=240,
                input_sha256=hashlib.sha256(content).hexdigest(), events_sha256=hashlib.sha256(events).hexdigest(),
                note='Observe-only termination probe. Save window tree/screenshots; never dismiss any dialog; no production histories.')
    path = RUN / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan: raise ValueError('Frozen lifecycle probe changed')
    if not path.exists(): write_json(path, plan)
    return plan, content, events


def worker(runtime):
    from PIL import ImageGrab
    runner = bulk.d.pysrim_runner
    original = runner.check_trim_dialogs
    started = time.monotonic()
    counter = 0
    def observe():
        nonlocal counter
        tree = subprocess.check_output(['xwininfo', '-root', '-tree'], text=True, timeout=5)
        if 'End of TRIM.DAT calculation' in tree or time.monotonic() - started > 75:
            atomic_text(runtime / 'terminal-window-tree.txt', tree)
            ImageGrab.grab(xdisplay=os.environ['DISPLAY']).save(runtime / 'terminal-screen.png')
            raise RuntimeError('TERMINAL_UI_OBSERVED_NO_ACTION: inspect screenshot and window tree')
        counter += 1
        return original()
    runner.check_trim_dialogs = observe
    runner.configure_recoil_capacity(runtime)
    runner.monitored_trim_run(bulk.d.ArchivedInput(), runtime, dict(description='mode5-one-ion-observe-only'))


def run():
    group = Path(resource_sample()['cgroup'])
    if group.name != UNIT or len(os.sched_getaffinity(0)) > 1 or (group / 'memory.max').read_text().strip() != str(8 * GIB) or (group / 'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('Use the isolated lifecycle service: one core, 8 GiB, zero swap')
    if (RUN / 'result.json').exists(): raise RuntimeError('One-shot probe already completed; review instead of overwriting')
    plan, content, events = prepare()
    cfg = scan.load_config(bulk.d.CONFIG); context = scan.resolve_execution_context(cfg)
    source = scan.resolve_srim_root(None, cfg)
    before = scan.source_control_fingerprint(source)
    runtimes, _ = scan.prepare_runtimes(source, RUN, 1, context); runtime = runtimes[0]
    origin = Path(plan['source'])
    (runtime / 'TRIM.IN').write_bytes(content); (runtime / 'TRIM.dat').write_bytes(events)
    (runtime / 'TRIMAUTO').write_bytes((origin / 'TRIMAUTO').read_bytes())
    (runtime / 'Data/TRIM.cfg').write_bytes((origin / 'TRIM.cfg').read_bytes())
    env = scan.pysrim_environment(scan.runtime_execution_context(context, RUN, runtime))
    result = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), runtime=str(runtime))
    write_json(RUN / 'status.json', result)
    start = time.monotonic()
    try:
        scan.launch_command(['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                             sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime)],
                            runtime, 120, environment=env, process_group=True)
        result['status'] = 'EXITED_WITHOUT_UI_OBSERVATION'
    except Exception as exc:
        result.update(status='UI_CAPTURED_REVIEW_REQUIRED' if (runtime / 'terminal-screen.png').exists() else 'NEEDS_ATTENTION', error=str(exc))
    finally:
        for option in ('-k', '-w'):
            try: subprocess.run(['wineserver', option], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired): pass
        out = RUN / 'artifacts'; out.mkdir(exist_ok=True)
        for name, path in scan.collect_runtime_outputs(runtime).items(): shutil.copy2(path, out / name)
        for name in ('TRIM.IN', 'TRIM.dat', 'TRIMAUTO', 'terminal-window-tree.txt', 'terminal-screen.png', '.trim-progress.json', '.trim-process.log', '.xvfb.log'):
            if (runtime / name).exists(): shutil.copy2(runtime / name, out / name)
        result.update(wall_seconds=time.monotonic() - start, source_unchanged=scan.source_control_fingerprint(source) == before,
                      resources=resource_sample(), artifact_sha256={str(p): scan.sha256_file(p) for p in out.iterdir() if p.is_file()})
        write_json(RUN / 'result.json', result); write_json(RUN / 'status.json', result)
        body = '<!doctype html><meta charset="utf-8"><h1>Mode-5 lifecycle observation</h1><p>No dialog was dismissed; no production data.</p>'
        body += table([result], list(result)) + '<p><a href="artifacts/terminal-window-tree.txt">Window tree</a></p><img src="artifacts/terminal-screen.png">'
        atomic_text(RUN / 'index.html', body)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker'))
    parser.add_argument('--runtime', type=Path)
    args = parser.parse_args()
    if args.command == 'worker': worker(args.runtime)
    elif args.command == 'prepare': print(json.dumps(prepare()[0], indent=2))
    else: run()
