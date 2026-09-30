#!/usr/bin/env python3
"""Replay-gated matched-seed pressure/binning diagnostics, never production."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan as scan
import pysrim_runner
from scripts import corrected_closeout as prior
from scripts.binding_probe import ArchivedInput
from scripts.repro_check import archive_data
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

RUN = ROOT / 'runs/kr_pressure_diagnostic'
UNIT = 'srim-pressure-diagnostic.service'
BUDGET = 7200
WORKERS = 15
CONFIG = prior.CONFIG
OUTPUTS = ('RANGE.TXT', 'IONIZ.TXT', 'TDATA.TXT')


def variant(original, name, pressure, width_factor=1, view_factor=1):
    """Scale density and geometry from a 1-atm input; keep seed/physics fixed."""
    if pressure <= 0 or width_factor < view_factor or view_factor <= 0:
        raise ValueError('Invalid pressure/geometry')
    lines = original.decode('ascii').splitlines()
    i = next(i for i, line in enumerate(lines) if line.startswith('1 "'))
    layer = shlex.split(lines[i])
    width, density = float(layer[2]), float(layer[3])
    lines[i] = (f'1 "kr80_cf4_20_p{pressure:g}atm" {width / pressure * width_factor:.17g} '
                f'{density * pressure:.17g} ' + ' '.join(layer[4:]))
    plot = next(i for i, line in enumerate(lines) if line.startswith('PlotType')) + 1
    lines[plot] = f'5 0.0 {width / pressure * view_factor:.17g}'
    target = lines.index('Target material : Number of Elements, Number of Layers') + 1
    lines[target] = f'"{name}" 3 1'
    return ('\r\n'.join(lines) + '\r\n').encode('ascii')


def prepare(run):
    prior.prepare(prior.RUN)
    completion = json.loads((prior.RUN / 'completion.json').read_text())
    if completion['summary']['points_complete'] != 243 or completion['summary']['independent_pressure_flags'] != 1:
        raise ValueError('Expected reviewed completed closeout')
    rows = [r for r in prior.fresh_rows(prior.RUN) if r['family'] == 'Kr' and
            r['noble_percent'] == 80 and r['ion'] == 'F' and r['energy_kev'] == 100]
    records = {(r['pressure_atm'], r['repeat']): r for r in rows}
    cases, inputs, paths = [], {}, {Path(__file__), ROOT / 'scripts/pressure_diagnostic.sh',
                                   ROOT / 'scripts/binding_probe.py', ROOT / 'pysrim_runner.py',
                                   CONFIG, prior.RUN / 'completion.json', prior.RUN / 'plan.json'}
    def add(name, phase, row, pressure, wf=1, vf=1, replay=False):
        source = Path(row['raw_archive'])
        paths.add(source)
        paths.add(Path(row['source_run']) / 'job_results/trim' / f"{row['job_id']}.json")
        original = archive_data(source, 'TRIM.IN')
        content = original if replay else variant(original, name, pressure, wf, vf)
        inputs[name] = content
        cases.append(dict(name=name, phase=phase, source=str(source), pressure_atm=pressure,
                          repeat=row['repeat'], seed=row['random_seed'], width_factor=wf,
                          view_factor=vf, input_sha256=hashlib.sha256(content).hexdigest(),
                          expected=row['target_energy_check']['expected'], ions=50))
    for p in (.5, 1., 3.):
        add(f'replay_p{p:g}', 'replay', records[p, 100], p, replay=True)
    for repeat in (100, 101, 102):
        for p in (.5, 1., 2.2, 2.3, 3.):
            add(f'paired_r{repeat}_p{p:g}', 'paired', records[1., repeat], p)
        for p in (.5, 1., 3.):
            for wf, vf in ((2, 1), (2, 2)):
                add(f'geometry_r{repeat}_p{p:g}_w{wf}_v{vf}', 'geometry', records[1., repeat], p, wf, vf)
    assert len(cases) == 36 and len(inputs) == 36
    cfg = scan.load_config(CONFIG)
    source = scan.resolve_srim_root(None, cfg)
    execution = scan.execution_provenance(scan.resolve_execution_context(cfg))
    fingerprint = dict(controls=scan.source_control_fingerprint(source), data=scan.source_fingerprint(source))
    original_provenance = json.loads((ROOT / 'runs/binding_validation/source_provenance.json').read_text())
    if original_provenance != dict(source=fingerprint, execution=execution):
        raise ValueError('Validated source/execution changed')
    plan = dict(cases=cases, input_sha256={str(p): scan.sha256_file(p) for p in sorted(paths)},
                source=fingerprint, execution=execution, model_id=prior.MODEL,
                total_cases=36, diagnostic_histories=1800, independent_production_histories_added=0,
                workers=WORKERS, budget_seconds=BUDGET, planning_minutes=60,
                gate='All three exact replays must match RANGE/IONIZ/TDATA byte-for-byte before variants.',
                design='Three archived 1-atm seeds crossed with five pressures, exact inverse-pressure '
                       'depth scaling; at .5/1/3 atm double target depth with unchanged and doubled viewing depth. '
                       '2.2/2.3 atm bracket the printed atomic-density exponent transition.',
                limitations='Exploratory numerical diagnostic, not independent precision/equivalence evidence. '
                            'Surface injection remains unchanged: does not simulate an internal bulk-gas recoil. '
                            'No automatic production release or extra cases.')
    path = run / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen diagnostic inputs changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs


def cached_result(case):
    path = RUN / 'cases' / case['name'] / 'result.json'
    if not path.exists():
        return None
    result = json.loads(path.read_text())
    if result['status'] == 'FAILED':
        raise RuntimeError(f"Inspect failed case before resuming: {case['name']}")
    if result['status'] != 'COMPLETE':
        return None
    if result['input_sha256'] != case['input_sha256']:
        raise ValueError('Cached input identity changed')
    for path, digest in result['artifact_sha256'].items():
        if scan.sha256_file(Path(path)) != digest:
            raise ValueError('Cached output artifact changed')
    return result


def run_case(case, content, runtime, context, deadline):
    started = time.monotonic()
    directory = RUN / 'cases' / case['name']
    directory.mkdir(parents=True, exist_ok=True)
    attempt = directory / f'attempt-{time.time_ns()}'
    attempt.mkdir()
    result = dict(case, status='RUNNING', attempt=str(attempt))
    env = scan.pysrim_environment(context)
    try:
        scan.clear_runtime_outputs(runtime)
        (runtime / 'TRIM.IN').write_bytes(content)
        (runtime / 'TRIMAUTO').write_bytes(archive_data(Path(case['source']), 'TRIMAUTO'))
        (runtime / 'Data/TRIM.cfg').write_bytes(archive_data(Path(case['source']), 'Data/TRIM.cfg'))
        (attempt / 'TRIM.IN').write_bytes(content)
        timeout = min(1200, int(deadline - time.monotonic() - 45))
        if timeout <= 0:
            raise RuntimeError('Cumulative deadline reached')
        command = ['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                   sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime),
                   '--name', case['name']]
        scan.launch_command(command, runtime, timeout, environment=env, process_group=True, display_name=case['name'])
        outputs = scan.collect_runtime_outputs(runtime)
        for name in scan.STANDARD_TRIM_OUTPUTS:
            scan.find_output(outputs, name)
        if (runtime / 'TRIM.IN').read_bytes() != content:
            raise ValueError('TRIM input changed during run')
        text = scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
        binding = scan.check_target_energies(text, case['expected'])
        if not binding['passed']:
            raise ValueError('Target energy mismatch')
        ions = re.search(r'Total Ions calculated\s*=\s*([\d.]+)', text)
        if ions is None or float(ions[1]) != 50:
            raise ValueError('Incomplete ion count')
        ranges = scan.parse_range_summary(scan.find_output(outputs, 'RANGE.TXT'))
        ioniz = scan.parse_ioniz(scan.find_output(outputs, 'IONIZ.TXT'), 100.)
        transmitted = scan.count_escape_records(scan.find_output(outputs, 'TRANSMIT.TXT'), 'T')
        back = scan.count_escape_records(scan.find_output(outputs, 'BACKSCAT.TXT'), 'B')
        plotline = next(i for i, line in enumerate(content.decode().splitlines()) if line.startswith('PlotType')) + 1
        view = float(content.decode().splitlines()[plotline].split()[2])
        if (not 0 < ioniz.qf_ioniz <= 1 or not math.isfinite(ranges.projected_range_mm)
                or ranges.projected_range_mm <= 0 or transmitted or ioniz.estimated_max_depth_angstrom >= .98 * view):
            raise ValueError('Numeric bounds/transmission/profile clipping failure')
        reported = float(re.search(r'Density\s*=\s*([\d.Ee+-]+) atoms/cm3', text)[1])
        expected_density = case['pressure_atm'] * 101325 / (1.380649e-23 * 293.15) / 1e6 * 1.8
        equality = {name: scan.find_output(outputs, name).read_bytes() == archive_data(Path(case['source']), name)
                    for name in OUTPUTS} if case['phase'] == 'replay' else {}
        result.update(status='COMPLETE', parameters_match=True, input_unchanged=True,
                      projected_range_mm=ranges.projected_range_mm,
                      pressure_times_range_mm_atm=ranges.projected_range_mm * case['pressure_atm'],
                      ionization_fraction=ioniz.qf_ioniz, primary_ionization_kev=ioniz.primary_ionization_energy_kev,
                      secondary_ionization_kev=ioniz.recoil_ionization_energy_kev,
                      reported_to_expected_atom_density=reported / expected_density,
                      reported_atoms_cm3=reported, expected_atoms_cm3=expected_density,
                      backscattered_ions=back, transmitted_ions=transmitted,
                      replay_file_equality=equality,
                      replay_passed=all(equality.values()) if equality else None)
    except Exception as exc:
        result.update(status='STOPPED' if scan.SHUTDOWN_REQUESTED.is_set() else 'FAILED', error=str(exc))
    finally:
        for option in ('-k', '-w'):
            try:
                subprocess.run(['wineserver', option], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for name, path in scan.collect_runtime_outputs(runtime).items():
            shutil.copy2(path, attempt / name)
        for name in ('TRIMAUTO', '.trim-progress.json', '.trim-process.log', '.xvfb.log'):
            if (runtime / name).exists():
                shutil.copy2(runtime / name, attempt / name)
        shutil.copy2(runtime / 'Data/TRIM.cfg', attempt / 'TRIM.cfg')
        result['artifact_sha256'] = {str(p): scan.sha256_file(p) for p in sorted(attempt.iterdir()) if p.is_file()}
        result['wall_seconds'] = time.monotonic() - started
        write_json(directory / 'result.json', result)
    return result


def replay_gate(results):
    return len(results) == 3 and all(r['status'] == 'COMPLETE' and r.get('replay_passed') for r in results)


def comparisons(results):
    lookup = {(r['repeat'], r['pressure_atm'], r['width_factor'], r['view_factor']): r
              for r in results if r['status'] == 'COMPLETE' and r['phase'] != 'replay'}
    out = []
    for key, row in sorted(lookup.items()):
        reference = lookup.get((key[0], 1. if row['phase'] == 'paired' else key[1], 1, 1))
        if reference is None:
            continue
        out.append(dict(name=row['name'], phase=row['phase'], repeat=key[0], pressure_atm=key[1],
                        reference=reference['name'],
                        qf_relative_difference=row['ionization_fraction'] / reference['ionization_fraction'] - 1,
                        pressure_range_relative_difference=row['pressure_times_range_mm_atm'] / reference['pressure_times_range_mm_atm'] - 1,
                        reported_to_expected_atom_density=row['reported_to_expected_atom_density'],
                        backscattered_ions=row['backscattered_ions']))
    return out


def publish(state, results):
    write_json(RUN / 'status.json', state)
    write_json(RUN / 'results.json', results)
    contrasts = comparisons(results)
    atomic_text(RUN / 'comparisons.csv', csv_text(contrasts, list(contrasts[0]) if contrasts else ['pending']))
    body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>Kr pressure diagnostics</title><style>body{font:16px system-ui;margin:30px}table{border-collapse:collapse}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>Kr / F / 100 keV — replay-gated pressure and geometry diagnostics</h1>
<p>36 fixed 50-ion cases. Three exact replays gate fifteen matched-seed pressure cases
and eighteen depth/binning variants. Three seeds reused deliberately: no independent
production histories, no equivalence claim, no automatic grid expansion.</p>
<p>0.5/1/2.2/2.3/3 atm, 80% Kr. Exact inverse-pressure target/view scaling.
At 0.5/1/3 atm, compare doubled target depth with unchanged or doubled view.
These tests still inject ions at the surface; internal bulk-recoil geometry is not implemented.</p>
<p>15 cores maximum, 24/32 GiB RAM, zero scan swap, two-hour cumulative cap.
<a href="plan.json">Frozen plan</a> · <a href="results.json">Results and raw paths</a> ·
<a href="comparisons.csv">Paired differences (descriptive)</a> · <a href="logs/campaign.log">Log</a></p>'''
    body += table([state], list(state))
    if results:
        body += table(results, ['name', 'status', 'replay_passed', 'ionization_fraction',
                                'pressure_times_range_mm_atm', 'reported_to_expected_atom_density', 'backscattered_ions'])
    atomic_text(RUN / 'index.html', body)


def execute():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS
            or (group / 'memory.max').read_text().strip() != str(32 * GIB)
            or (group / 'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use pressure_diagnostic.sh for resource isolation')
    if memory_info()['MemAvailable'] < 16 * GIB:
        raise RuntimeError('Insufficient available host RAM')
    used = json.loads((RUN / 'budget.json').read_text())['used_seconds'] if (RUN / 'budget.json').exists() else 0.
    if used >= BUDGET:
        raise RuntimeError('Cumulative budget exhausted')
    started = time.monotonic()
    deadline = started + BUDGET - used
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), completed_cases=0)
    results = []
    def stop(sig, frame):
        scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    def update():
        elapsed = used + time.monotonic() - started
        if elapsed >= BUDGET:
            scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(), cumulative_hours=elapsed / 3600,
                     completed_cases=sum(r['status'] == 'COMPLETE' for r in results), resources=resource_sample())
        write_json(RUN / 'budget.json', dict(used_seconds=elapsed, limit_seconds=BUDGET))
        publish(state, results)
    try:
        state['phase'] = 'Validate frozen plan and prepare isolated runtimes'
        update()
        plan, inputs = prepare(RUN)
        cfg = scan.load_config(CONFIG)
        context = scan.resolve_execution_context(cfg)
        source = scan.resolve_srim_root(None, cfg)
        runtimes, _ = scan.prepare_runtimes(source, RUN, WORKERS, context)
        write_json(RUN / f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase in ('replay', 'paired', 'geometry'):
            state['phase'] = phase
            pending = []
            for case in plan['cases']:
                if case['phase'] != phase:
                    continue
                cached = cached_result(case)
                if cached is not None:
                    results.append(cached)
                else:
                    pending.append(case)
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                available, active = list(runtimes), {}
                while pending or active:
                    while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline - time.monotonic() < 1245:
                            scan.SHUTDOWN_REQUESTED.set()
                            break
                        case, runtime = pending.pop(0), available.pop()
                        future = pool.submit(run_case, case, inputs[case['name']], runtime,
                                             scan.runtime_execution_context(context, RUN, runtime), deadline)
                        active[future] = runtime
                    if not active:
                        break
                    done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                    for future in done:
                        available.append(active.pop(future))
                        result = future.result()
                        results.append(result)
                        print(json.dumps(result), flush=True)
                        if result['status'] != 'COMPLETE':
                            scan.SHUTDOWN_REQUESTED.set()
                    update()
            if any(r['status'] == 'FAILED' for r in results):
                raise RuntimeError('Diagnostic case failed; inspect preserved artifacts')
            if scan.SHUTDOWN_REQUESTED.is_set():
                raise InterruptedError('Stopped or insufficient remaining cumulative budget')
            if phase == 'replay' and not replay_gate(results):
                raise RuntimeError('Exact replay gate failed; variants not released')
            update()
        prepare(RUN)
        if len(results) != 36 or int(dict(line.split() for line in (group / 'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('Case count or OOM validation failed')
        state.update(status='REVIEW_REQUIRED', phase='Fixed diagnostics complete; production remains on hold', source_unchanged=True)
        update()
        write_json(RUN / 'completion.json', state)
        return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update()
        print(str(exc), file=sys.stderr, flush=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker', 'status'))
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--name')
    args = parser.parse_args()
    if args.command == 'worker':
        pysrim_runner.configure_recoil_capacity(args.runtime)
        pysrim_runner.monitored_trim_run(ArchivedInput(), args.runtime, dict(description=args.name))
        return 0
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN / 'status.json').exists():
            print((RUN / 'status.json').read_text())
        print('Report:', RUN / 'index.html')
        return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN)
            print(json.dumps({k: v for k, v in plan.items() if k not in ('input_sha256', 'source', 'execution', 'cases')}, indent=2))
            return 0
        return execute()


if __name__ == '__main__':
    raise SystemExit(main())
