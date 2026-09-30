#!/usr/bin/env python3
"""One-ion normal-completion canary, separate from all production statistics."""
from __future__ import annotations
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mode5_lifecycle_probe as observation
from scripts import trim_dat_completion as completion
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, GIB
bulk, scan = observation.bulk, observation.scan
RUN = ROOT / 'runs/mode5_completion_canary'
UNIT = 'srim-mode5-completion.service'


def prepare():
    plan, content, events = observation.prepare()
    paths = [Path(__file__), Path(completion.__file__), Path(observation.__file__),
             observation.RUN / 'result.json']
    obs = json.loads(paths[-1].read_text())
    if obs['status'] != 'UI_CAPTURED_REVIEW_REQUIRED' or not obs['source_unchanged']:
        raise ValueError('Missing reviewed observe-only evidence')
    paths += [Path(p) for p, sha in obs['artifact_sha256'].items()
              if scan.sha256_file(Path(p)) == sha]
    if len(paths) != 4 + len(obs['artifact_sha256']): raise ValueError('Observation artifacts changed')
    plan = dict(plan, code_sha256=scan.sha256_file(Path(__file__)),
                input_files_sha256={str(p): scan.sha256_file(p) for p in paths},
                note='Acknowledge only the observed exact mode-5 completion title, once. '
                     'Require normal exit, all final outputs, executed parameters, one event and endpoint/escape accounting. '
                     'One diagnostic history, never production statistics.')
    path = RUN / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan: raise ValueError('Frozen completion canary changed')
    if not path.exists(): write_json(path, plan)
    return plan, content, events


def analyse(runtime):
    outputs = scan.collect_runtime_outputs(runtime)
    for name in (*scan.STANDARD_TRIM_OUTPUTS, 'RANGE_3D.TXT', 'BACKSCAT.TXT', 'TRANSMIT.TXT'):
        scan.find_output(outputs, name)
    old = json.loads(observation.FAILED.read_text())
    tdata = scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
    if not scan.check_target_energies(tdata, old['expected'])['passed']: raise ValueError('Bindings changed')
    count = re.search(r'Total Ions calculated\s*=\s*([\d.]+)', tdata)
    if count is None or float(count[1]) != 1: raise ValueError('Expected exactly one completed ion')
    q = scan.parse_ioniz(scan.find_output(outputs, 'IONIZ.TXT'), 100.)
    if not 0 < q.qf_ioniz <= 1: raise ValueError('Invalid ionization integral')
    back = scan.count_escape_records(scan.find_output(outputs, 'BACKSCAT.TXT'), 'B')
    transmitted = scan.count_escape_records(scan.find_output(outputs, 'TRANSMIT.TXT'), 'T')
    endpoints = bulk.endpoints(scan.find_output(outputs, 'RANGE_3D.TXT').read_text(encoding='latin1'),
                               1, back + transmitted, old['width_angstrom'], 1)
    ack = json.loads((runtime / 'completion-ack.json').read_text())
    if ack['status'] != 'sent' or ack['title'] != completion.TITLE: raise ValueError('Missing exact completion acknowledgement')
    return dict(ions_confirmed=1, parameters_match=True, ionization_fraction=q.qf_ioniz,
                backscattered_ions=back, transmitted_ions=transmitted, **endpoints)


def run():
    group = Path(resource_sample()['cgroup'])
    if group.name != UNIT or len(os.sched_getaffinity(0)) > 1 or (group / 'memory.max').read_text().strip() != str(8 * GIB) or (group / 'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('Use one-core completion service, 8 GiB and zero swap')
    if (RUN / 'result.json').exists(): raise RuntimeError('One-shot canary already finished')
    plan, content, events = prepare()
    cfg = scan.load_config(bulk.d.CONFIG); context = scan.resolve_execution_context(cfg)
    source = scan.resolve_srim_root(None, cfg); before = scan.source_control_fingerprint(source)
    runtime = scan.prepare_runtimes(source, RUN, 1, context)[0][0]
    origin = Path(plan['source'])
    (runtime / 'TRIM.IN').write_bytes(content); (runtime / 'TRIM.dat').write_bytes(events)
    for name, destination in [('TRIMAUTO', 'TRIMAUTO'), ('TRIM.cfg', 'Data/TRIM.cfg')]:
        (runtime / destination).write_bytes((origin / name).read_bytes())
    env = scan.pysrim_environment(scan.runtime_execution_context(context, RUN, runtime))
    result = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), runtime=str(runtime))
    write_json(RUN / 'status.json', result); started = time.monotonic()
    try:
        scan.launch_command(['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                             sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime)],
                            runtime, 120, environment=env, process_group=True)
        if (runtime / 'TRIM.IN').read_bytes() != content or (runtime / 'TRIM.dat').read_bytes() != events:
            raise ValueError('Executed input changed')
        result.update(analyse(runtime), status='COMPLETE')
        if scan.source_control_fingerprint(source) != before: raise ValueError('Source controls changed')
        prepare()
    except Exception as exc: result.update(status='NEEDS_ATTENTION', error=str(exc))
    finally:
        for option in ('-k', '-w'):
            try: subprocess.run(['wineserver', option], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired): pass
        out = RUN / 'artifacts'; out.mkdir(exist_ok=True)
        for name, path in scan.collect_runtime_outputs(runtime).items(): shutil.copy2(path, out / name)
        names = ['TRIM.IN', 'TRIM.dat', 'TRIMAUTO', '.trim-progress.json', '.trim-process.log', '.xvfb.log', 'completion-ack.json']
        names += [p.name for p in runtime.glob('*-screen.png')] + [p.name for p in runtime.glob('*-window-tree.txt')]
        for name in names:
            if (runtime / name).exists(): shutil.copy2(runtime / name, out / name)
        shutil.copy2(runtime / 'Data/TRIM.cfg', out / 'TRIM.cfg')
        result.update(wall_seconds=time.monotonic() - started, source_unchanged=scan.source_control_fingerprint(source) == before,
                      resources=resource_sample(), artifact_sha256={str(p): scan.sha256_file(p) for p in out.iterdir() if p.is_file()})
        write_json(RUN / 'result.json', result); write_json(RUN / 'status.json', result)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker')); parser.add_argument('--runtime', type=Path)
    args = parser.parse_args()
    if args.command == 'worker': completion.run(args.runtime, 'mode5-one-ion-completion-canary')
    elif args.command == 'prepare': print(json.dumps(prepare()[0], indent=2))
    else: raise SystemExit(run())
