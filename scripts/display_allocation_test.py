#!/usr/bin/env python3
"""Four synchronized 15-display waves; no Wine or transport simulations."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import private_xvfb as display
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, GIB
import srim_scan as scan
RUN = ROOT / 'runs/display_allocation_test'
UNIT = 'srim-display-allocation-test.service'


def run():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15 or
            (group / 'memory.max').read_text().strip() != str(8 * GIB) or
            (group / 'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bounded display test service: 15 cores, 8 GiB, zero swap')
    if (RUN / 'result.json').exists(): raise RuntimeError('Test already completed; do not overwrite')
    RUN.mkdir(parents=True, exist_ok=True)
    started = time.monotonic(); rows = []
    paths = [Path(__file__), Path(display.__file__), Path('/usr/bin/Xvfb'), Path('/usr/bin/xauth')]
    write_json(RUN / 'plan.json', dict(waves=4, displays_per_wave=15, startup_timeout_seconds=20,
                                     no_transport_histories=True,
                                     input_sha256={str(p):scan.sha256_file(p) for p in paths}))
    try:
        for wave in range(4):
            barrier = threading.Barrier(15, timeout=40)
            def check(worker):
                directory = RUN / f'wave-{wave}' / f'worker-{worker:03d}'; directory.mkdir(parents=True)
                with display.private_display(directory) as (env, server, metadata):
                    barrier.wait()
                    for _ in range(3):
                        subprocess.run(['xwininfo', '-root', '-tree'], env=env, capture_output=True, check=True, timeout=5)
                    # Access must fail with an empty authority file, not just succeed with ours.
                    empty = directory / 'empty-authority'; empty.touch(mode=0o600)
                    denied = subprocess.run(['xwininfo', '-root'], env=dict(env, XAUTHORITY=str(empty)), capture_output=True, timeout=5)
                    if denied.returncode == 0: raise RuntimeError('Display accepted unauthenticated client')
                    barrier.wait()
                    row = dict(wave=wave, worker=worker, **metadata, unauthorized_client_rejected=True)
                if server.poll() is None: raise RuntimeError('Xvfb leaked after context exit')
                return dict(row, server_reaped=True)
            with ThreadPoolExecutor(max_workers=15) as pool: batch = list(pool.map(check, range(15)))
            if len({r['display'] for r in batch}) != 15: raise ValueError('Live display allocation collision')
            rows.extend(batch)
            write_json(RUN / 'status.json', dict(status='RUNNING', completed_displays=len(rows), expected_displays=60))
        result = dict(status='COMPLETE', checks=rows, completed_displays=len(rows),
                      wall_seconds=time.monotonic()-started, resources=resource_sample())
    except Exception as exc:
        result = dict(status='NEEDS_ATTENTION', error=str(exc), checks=rows,
                      wall_seconds=time.monotonic()-started, resources=resource_sample())
    result['artifact_sha256'] = {str(p):scan.sha256_file(p) for p in RUN.glob('wave-*/*/*') if p.is_file()}
    write_json(RUN / 'result.json', result); write_json(RUN / 'status.json', {k:v for k,v in result.items() if k not in ('checks','artifact_sha256')})
    print(json.dumps({k:v for k,v in result.items() if k not in ('checks','artifact_sha256')}, indent=2))
    return 0 if result['status']=='COMPLETE' else 1


if __name__ == '__main__': raise SystemExit(run())
