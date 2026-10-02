#!/usr/bin/env python3
"""C1d: bounded display validation and one exact replay after the OS upgrade."""
import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import display_allocation_test as stress
from scripts import high_energy_pressure as worker
from scripts import historical_execution as history
from scripts.preliminary_report import atomic_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

b, scan = worker.b, worker.scan
RUN = ROOT / 'runs/xvfb_upgrade_gate'
UNIT = 'srim-xvfb-upgrade-gate.service'
BUDGET = 300


def prepare(run):
    transition = history.evidence()
    with history.historical_sources():
        pilot_plan, pilot_inputs = b.p.prepare(b.p.RUN)
        original = next(c for c in pilot_plan['cases'] if c['family'] == 'Kr' and c['ion'] == 'F'
                        and c['energy_kev'] == 1. and c['repeat'] == 0)
        reference = json.loads((b.p.RUN/'cases'/original['name']/'result.json').read_text())
        b.previous.audit_case(original, reference, pilot_inputs[original['name']])
    content, events = pilot_inputs[original['name']]
    case = dict(original, phase='environment_replay', diagnostic=True,
                replay_reference=reference['attempt'], density_g_cm3=float(worker.layer_fields(content)[3]))
    config = scan.load_config(b.base.d.CONFIG); srim = scan.resolve_srim_root(None, config)
    paths = {Path(__file__), ROOT/'scripts/xvfb_upgrade_gate.sh', Path(history.__file__), history.EVIDENCE, history.HISTORICAL,
             Path(stress.__file__), Path(stress.display.__file__), Path(worker.r.worker.__file__),
             Path('/usr/bin/Xvfb'), Path('/usr/bin/xauth'), Path('/usr/bin/xwininfo'),
             Path('/usr/bin/wine'), Path('/usr/bin/wineserver'), srim/'TRIM.exe', srim/'Data/ATOMDATA',
             b.p.RUN/'plan.json', b.p.RUN/'cases'/original['name']/'result.json'}
    paths.update(Path(p) for p in reference['artifact_sha256'])
    paths.update(Path(module.__file__) for module in
                 (worker, b, b.base, b.p, worker.r, worker.r.worker.legacy, scan))
    paths.update(ROOT/name for name in ('pysrim_runner.py', 'srim_gas_sweep.py', 'trim_qf_sweep.py',
                                        'scripts/staged_scan.py', 'scripts/timing_pilot.py',
                                        'scripts/preliminary_report.py'))
    paths.add(Path(b.base.d.CONFIG))
    plan = dict(stage='C1d', environment_transition=transition, current_execution_sha256={str(p): history.actual_sha256(p) for p in sorted(paths)},
                replay_case=case, replay_histories=50, new_independent_production_histories=0,
                display_waves=4, displays_per_wave=15, workers_and_cores_max=15, memory_max_gib=8, swap_max_gib=0,
                budget_seconds=BUDGET, expected_compute_minutes=[1, 3],
                gates='60 concurrent authenticated display startups, unique live allocation, unauthorized-client rejection and cleanup. '
                      'One exact Kr/F1keV50-ion bulk replay: identical TRIM.IN/TRIM.DAT and IONIZ/RANGE/RANGE_3D/TDATA bytes, '
                      'normal completion, actual parameters, fifty contained endpoints, zero escapes/edge signal and current helper evidence. '
                      'Current executable/source hashes checked before and after. Historical fingerprints verified against recovered '
                      'hash-identical bytes only within an explicit historical-audit context.',
                next='Stop for user review before M3. No OS downgrade, frozen-file edit, new production seed or automatic mixture launch.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen environment gate changed')
    if not path.exists():
        write_json(path, plan)
    return plan, (content, events)


def run():
    group = Path(resource_sample()['cgroup'])
    if group.name != UNIT or len(os.sched_getaffinity(0)) > 15 or (group/'memory.max').read_text().strip() != str(8*GIB) or (group/'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('Use the bounded environment-gate launcher')
    if (RUN/'completion.json').exists():
        raise RuntimeError('Already complete; review instead of rerunning')
    if memory_info()['MemAvailable'] < 12*GIB:
        raise RuntimeError('Insufficient available RAM')
    started = time.monotonic(); state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), phase='preparing')
    def update():
        state.update(updated_at=datetime.now().astimezone().isoformat(), wall_seconds=time.monotonic()-started, resources=resource_sample())
        write_json(RUN/'status.json', state)
        body = '<html><meta charset="utf-8"><meta http-equiv="refresh" content="15"><title>Post-upgrade environment gate</title>'
        body += '<h1>C1d: current Xvfb validation and exact replay</h1><p>Five-minute budget, 15-core maximum, 8 GiB RAM, zero swap. '
        body += '60 display checks and one diagnostic 50-ion replay; no production histories or automatic M3 launch.</p>'
        body += table([state], list(state)); atomic_text(RUN/'index.html', body)
    try:
        update(); plan, inputs = prepare(RUN); state['phase'] = 'display_stress'; update()
        old_run, old_unit = stress.RUN, stress.UNIT
        try:
            stress.RUN, stress.UNIT = RUN/'display-stress', UNIT
            if stress.run() != 0:
                raise RuntimeError('Current display startup/isolation test failed')
        finally:
            stress.RUN, stress.UNIT = old_run, old_unit
        state.update(display_checks_passed=60, phase='exact_bulk_replay'); update()
        config = scan.load_config(b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, 1, context)
        write_json(RUN/f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        runtime = runtimes[0]; old_b, old_base, old_launch = b.RUN, b.base.RUN, scan.launch_command
        try:
            b.RUN, b.base.RUN, scan.launch_command = RUN, RUN, worker.r.launch
            row = b.run_case(plan['replay_case'], inputs, runtime, scan.runtime_execution_context(context, RUN, runtime), started+BUDGET)
        finally:
            b.RUN, b.base.RUN, scan.launch_command = old_b, old_base, old_launch
        metadata = runtime/'.completion-probes.json'
        if not metadata.exists():
            raise ValueError('Missing current completion-helper evidence')
        target = Path(row['attempt'])/metadata.name; shutil.copy2(metadata, target)
        row['artifact_sha256'][str(target)] = history.actual_sha256(target); row['completion_monitor'] = json.loads(target.read_text())
        row.update(worker.check_density(plan['replay_case'], scan.collect_runtime_outputs(runtime)))
        write_json(RUN/'cases'/row['name']/'result.json', row); write_json(RUN/'replay.json', row)
        worker.audit_case(plan['replay_case'], row, inputs)
        if row['status'] != 'COMPLETE' or not row.get('replay_passed'):
            raise ValueError('Exact post-upgrade replay did not pass')
        prepare(RUN)
        for path, digest in plan['current_execution_sha256'].items():
            if history.actual_sha256(path) != digest:
                raise ValueError('Current environment/source changed during validation')
        if time.monotonic()-started > BUDGET:
            raise RuntimeError('Environment validation budget exceeded')
        state.update(status='REVIEW_REQUIRED', phase='Environment checks complete; review before M3',
                     source_unchanged=True, exact_replay_passed=True, replay_histories=50,
                     new_independent_production_histories=0, replay_wall_seconds=row['wall_seconds'])
        update(); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='NEEDS_ATTENTION', error=str(exc)); update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists(): print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN); print(json.dumps({k: plan[k] for k in ('stage', 'expected_compute_minutes', 'budget_seconds')}, indent=2)); return 0
        return run()


if __name__ == '__main__':
    raise SystemExit(main())
