#!/usr/bin/env python3
"""C2c: fixed cross-family, low/high-energy contained-start validation; not production."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import fcntl
import hashlib
import heapq
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
from scripts import review_bulk_geometry as previous
from scripts import trim_dat_completion as completion
from scripts import campaign_schedule as schedule
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB
from trim_qf_sweep import numeric_columns_from_line

retry = previous.prior
scan, d, bulk = retry.scan, retry.d, retry.bulk
RUN = ROOT / 'runs/noble_cf4_bulk_corners'
UNIT = 'srim-bulk-corners.service'
BUDGET = 6 * 3600
JOB_TIMEOUT = 3 * 3600
WORKERS = 15
MODEL = 'candidate-trim5-interior-eps1e9-v1'
QF_DEPTH_MARGIN = .002
RANGE_DEPTH_MARGIN = .005


def event_file(z, energy, depth):
    if z not in (6, 9, 18, 36, 54) or energy not in (1., 1000.) or not math.isfinite(depth) or depth <= 0:
        raise ValueError('Unexpected corner ion, energy or origin')
    headers = ['TRIM.DAT cross-family bulk-corner validation', 'Ten comment lines; CRLF and DOS EOF',
               'Full-cascade mode 5; diagnostic only', 'Exactly fifty primary ions',
               'Energy in eV, positions in Angstrom', 'Direction +X; Y=Z=0',
               'Five-character event identity', f'Z={z} energy={energy:g}keV origin={depth:.17g}A',
               'Event Z Energy X Y Z CosX CosY CosZ', 'BEGIN EVENTS AFTER THIS TENTH HEADER LINE']
    events = [f'E{i:04d} {z} {energy * 1000:.17g} {depth:.17g} 0 0 1 0 0' for i in range(1, 51)]
    return ('\r\n'.join(headers + events) + '\r\n').encode('ascii') + b'\x1a'


def make_input(original, name, factor, seed, z, energy):
    if factor not in (2, 4) or not 0 < seed < 1000000000: raise ValueError('Invalid corner depth/seed')
    lines = original.decode('ascii').splitlines()
    ion = lines[next(i for i, x in enumerate(lines) if x.startswith('Ion:')) + 1].split()
    if int(ion[0]) != z or float(ion[2]) != energy or int(ion[4]) != 50: raise ValueError('Source ion/energy/count mismatch')
    index = next(i for i, x in enumerate(lines) if x.startswith('1 "'))
    layer = shlex.split(lines[index]); width = float(layer[2]) * factor
    lines[index] = f'1 "{layer[1]}" {width:.17g} ' + ' '.join(layer[3:])
    index = next(i for i, x in enumerate(lines) if x.startswith('Cascades(')) + 1
    fields = lines[index].split(); fields[0:2] = ['5', str(seed)]; lines[index] = ' '.join(fields)
    index = next(i for i, x in enumerate(lines) if x.startswith('Diskfiles')) + 1
    fields = lines[index].split(); fields[0] = '1'; lines[index] = ' '.join(fields)
    index = next(i for i, x in enumerate(lines) if x.startswith('PlotType')) + 1
    lines[index] = f'5 0.0 {width:.17g}'
    index = lines.index('Target material : Number of Elements, Number of Layers') + 1
    lines[index] = f'"{name}" 3 1'
    return ('\r\n'.join(lines) + '\r\n').encode('ascii'), event_file(z, energy, width / 2), width / 2, width


def prepare(run):
    review, old, *_ = previous.review()
    rows = d.prior.previous.read_rows(d.prior.previous.RUN) + d.prior.fresh_rows(d.prior.RUN)
    records = {}
    used_seeds = {r['random_seed'] for r in rows}
    for row in sorted(rows, key=lambda r: r['repeat']):
        if row['noble_percent'] == 80 and row['pressure_atm'] == 1 and row['energy_kev'] in (1, 1000):
            records.setdefault((row['family'], row['ion'], row['energy_kev']), row)
    if len(records) != 18: raise ValueError('Expected eighteen corner points')
    paths = {Path(__file__), ROOT / 'scripts/bulk_corner_validation.sh', Path(previous.__file__),
             Path(completion.__file__), Path(schedule.__file__), retry.RUN / 'plan.json', retry.RUN / 'completion.json',
             retry.RUN / 'results.json', schedule.TIMINGS}
    cases, inputs = [], {}
    reference = next(r for r in old if r['name'] == 'interior_r100_p1')
    directory = Path(reference['attempt'])
    content, events = (directory / 'TRIM.IN').read_bytes(), (directory / 'TRIM.DAT').read_bytes()
    name = 'replay_kr_f100_interior'
    inputs[name] = (content, events)
    paths.update(Path(p) for p in reference['artifact_sha256'])
    paths.add(retry.RUN / 'cases' / reference['name'] / 'result.json')
    cases.append(dict(name=name, phase='canary', geometry='interior', family='Kr', ion='F', ion_z=9, energy_kev=100.,
                      pressure_atm=1., ions=50, seed=reference['seed'], source=reference['source'],
                      origin_angstrom=reference['origin_angstrom'], width_angstrom=reference['width_angstrom'],
                      expected=reference['expected'], input_sha256=scan.sha256_file(directory / 'TRIM.IN'),
                      trim_dat_sha256=scan.sha256_file(directory / 'TRIM.DAT'), replay_reference=str(directory),
                      estimated_seconds=reference['wall_seconds']))
    means = schedule.timing_means(schedule.TIMINGS)
    for (family, ion, energy), row in sorted(records.items()):
        point = f'{family}_{ion}_e{energy:g}'
        seed = scan.seed_for('bulk-corner-validation-v1-' + point, 0)
        if seed in used_seeds: raise ValueError('Fresh corner seed collision')
        used_seeds.add(seed)
        source = Path(row['raw_archive']); paths.add(source)
        for factor in (2, 4):
            name = f'{point}_w{factor}'
            content, events, origin, width = make_input(d.archive_data(source, 'TRIM.IN'), name, factor,
                                                        seed, row['ion_z'], energy)
            inputs[name] = (content, events)
            cases.append(dict(name=name, phase='canary' if energy == 1 else 'high_energy',
                              geometry='interior' if factor == 2 else 'interior_deep', point=point,
                              family=family, ion=ion, ion_z=row['ion_z'], energy_kev=energy, pressure_atm=1., ions=50, seed=seed,
                              source=str(source), origin_angstrom=origin, width_angstrom=width,
                              expected=row['target_energy_check']['expected'],
                              input_sha256=hashlib.sha256(content).hexdigest(),
                              trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                              estimated_seconds=means[family, ion, energy, 80, 1.]))
    phases = {}
    for phase in ('canary', 'high_energy'):
        costs = [0.] * WORKERS
        for duration in sorted((c['estimated_seconds'] for c in cases if c['phase'] == phase), reverse=True):
            heapq.heapreplace(costs, costs[0] + duration)
        phases[phase] = max(costs) / 3600
    forecast = dict(model_hours=sum(phases.values()), planning_hours=1.5 * sum(phases.values()) + .5,
                    phase_model_hours=phases, longest_reference_batch_hours=max(c['estimated_seconds'] for c in cases) / 3600,
                    assumptions='Measured 50-ion mode-2 corner times plus measured mode-5 replay time, 15 workers, '
                    'sequential low/replay and high-energy gates; 50% margin plus 30 minutes. '
                    'Interior heavy-recoil timing not yet measured. Not a guarantee.')
    if len(cases) != 37 or forecast['planning_hours'] > 4: raise ValueError('Count/forecast exceeds frozen design')
    plan = dict(stage='C2c', model_id=MODEL, production_model_unchanged=d.prior.MODEL, cases=cases,
                total_cases=37, diagnostic_histories=1850, independent_production_histories_added=0,
                unique_fresh_seeds=18, workers=WORKERS, budget_hours=6, job_timeout_seconds=JOB_TIMEOUT,
                depth_qf_relative_margin=QF_DEPTH_MARGIN, depth_dx_relative_margin=RANGE_DEPTH_MARGIN,
                previous_review=review, forecast=forecast,
                input_sha256={str(p): scan.sha256_file(p) for p in sorted(paths)},
                design='One exact Kr/F100keV interior replay and 18 low-energy cases must pass before 18 high-energy cases. '
                       'Ar/Kr/Xe, C/F/noble, 80% noble, 1 atm, 1keV and 1MeV; one fresh seed per point shared across '
                       'two central-start depths (2W/4W). 36 corner cases plus one replay; 50 ions each.',
                gates='All final outputs/inputs/bindings/counts/acknowledgements audited. No primary escape, all 50 endpoints, '
                      'zero edge-bin ionization. Range is endpoint X minus origin, not absolute RANGE.TXT. '
                      'Exact replay matches IONIZ/RANGE/RANGE_3D/TDATA bytes. Paired-depth relative changes must be <=0.2% '
                      'for ionization and <=0.5% for mean displacement. These conservative numerical screening margins '
                      'are below production SEM targets, not confidence intervals or equivalence certificates. '
                      'On failure stop submissions; no automatic depth enlargement, new seeds or production release.',
                limitations='One fresh 50-ion sample per corner, reused across depth pair; not two independent batches. '
                            'No precision, pressure-equivalence, mixture-wide validation or detector-quenching claim. '
                            'Review model compatibility and a separately validated baseline rebuild before full production.')
    path = run / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan: raise ValueError('Frozen corner plan changed')
    if not path.exists(): write_json(path, plan)
    return plan, inputs


def analyse(case, outputs):
    for name in (*scan.STANDARD_TRIM_OUTPUTS, 'RANGE_3D.TXT', 'BACKSCAT.TXT', 'TRANSMIT.TXT'):
        scan.find_output(outputs, name)
    tdata = scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
    if not scan.check_target_energies(tdata, case['expected'])['passed']: raise ValueError('Target parameter mismatch')
    ion = re.search(r'Ion\s*=\s*(\w+)\s*\(\s*(\d+)\)', tdata)
    energy = re.search(r'Energy\s*=\s*([\d.Ee+\-]+)\s*keV', tdata)
    if (ion is None or ion[1] != case['ion'] or int(ion[2]) != case['ion_z'] or energy is None or
            not math.isclose(float(energy[1]), case['energy_kev'], rel_tol=1e-6)):
        raise ValueError('Executed primary ion/energy mismatch')
    count = re.search(r'Total Ions calculated\s*=\s*([\d.]+)', tdata)
    if count is None or float(count[1]) != 50: raise ValueError('Incomplete primary ion count')
    q = scan.parse_ioniz(scan.find_output(outputs, 'IONIZ.TXT'), case['energy_kev'])
    back = scan.count_escape_records(scan.find_output(outputs, 'BACKSCAT.TXT'), 'B')
    transmitted = scan.count_escape_records(scan.find_output(outputs, 'TRANSMIT.TXT'), 'T')
    if back or transmitted: raise ValueError('Interior primary escaped')
    if not 0 < q.qf_ioniz <= 1 or q.estimated_max_depth_angstrom >= .98 * case['width_angstrom']:
        raise ValueError('Invalid ionization integral or upper profile clipping')
    profile = [v for line in scan.find_output(outputs, 'IONIZ.TXT').read_text(encoding='latin1').splitlines()
               if (v := numeric_columns_from_line(line)) is not None]
    totals = [v[1] + v[2] for v in profile]
    if len(totals) != 100 or max(totals[0], totals[-1]) > max(totals) * 1e-9:
        raise ValueError('Ionization profile reaches a boundary')
    result = bulk.endpoints(scan.find_output(outputs, 'RANGE_3D.TXT').read_text(encoding='latin1'),
                            50, 0, case['width_angstrom'], case['origin_angstrom'])
    if result['stopped_ion_mean_dx_mm'] <= 0: raise ValueError('Nonpositive mean displacement; review before release')
    reported = scan.parse_range_summary(scan.find_output(outputs, 'RANGE.TXT')).projected_range_mm
    if reported is None or not math.isclose(reported, result['endpoint_mean_x_mm'], rel_tol=.001):
        raise ValueError('Raw range summary does not match absolute endpoints')
    if 'replay_reference' in case:
        for name in ('RANGE.TXT', 'IONIZ.TXT', 'RANGE_3D.TXT', 'TDATA.TXT'):
            if scan.find_output(outputs, name).read_bytes() != (Path(case['replay_reference']) / name).read_bytes():
                raise ValueError('Exact interior replay differs: ' + name)
        result['replay_passed'] = True
    result.update(ionization_fraction=q.qf_ioniz, reported_range_summary_mm=reported,
                  backscattered_ions=back, transmitted_ions=transmitted, ions_confirmed=50, parameters_match=True,
                  primary_ionization_kev=q.primary_ionization_energy_kev, secondary_ionization_kev=q.recoil_ionization_energy_kev)
    return result


def depth_checks(results):
    by_point = {}
    for r in results:
        if r['status'] == 'COMPLETE' and 'point' in r: by_point.setdefault(r['point'], {})[r['geometry']] = r
    comparisons = []
    for point, pair in sorted(by_point.items()):
        if set(pair) != {'interior', 'interior_deep'}: continue
        a, b = pair['interior'], pair['interior_deep']
        if a['seed'] != b['seed']: raise ValueError('Depth pair seed mismatch')
        qf = b['ionization_fraction'] / a['ionization_fraction'] - 1
        dx = b['stopped_ion_mean_dx_mm'] / a['stopped_ion_mean_dx_mm'] - 1
        comparisons.append(dict(point=point, qf_relative_change=qf, dx_relative_change=dx,
                                passed=abs(qf) <= QF_DEPTH_MARGIN and abs(dx) <= RANGE_DEPTH_MARGIN))
    return comparisons


def cached(case):
    path = RUN / 'cases' / case['name'] / 'result.json'
    if not path.exists(): return None
    r = json.loads(path.read_text())
    if r['status'] == 'FAILED': raise RuntimeError('Review failed case before resuming: ' + case['name'])
    if r['status'] != 'COMPLETE': return None
    if any(r[k] != v for k, v in case.items()): raise ValueError('Cached case identity changed')
    for p, digest in r['artifact_sha256'].items():
        if scan.sha256_file(Path(p)) != digest: raise ValueError('Cached artifacts changed')
    return r


def run_case(case, contents, runtime, context, deadline):
    started = time.monotonic(); directory = RUN / 'cases' / case['name']; directory.mkdir(parents=True, exist_ok=True)
    attempt = directory / f'attempt-{time.time_ns()}'; attempt.mkdir()
    result = dict(case, status='RUNNING', attempt=str(attempt)); env = scan.pysrim_environment(context)
    try:
        scan.clear_runtime_outputs(runtime)
        # Previous evidence is already archived; clean only private-worker scratch files.
        for pattern in ('completion-ack.json', '*-screen.png', '*-window-tree.txt', '.trim-progress.json', '.trim-dialog.png'):
            for p in runtime.glob(pattern):
                if p.is_file(): p.unlink()
        content, events = contents
        (runtime / 'TRIM.IN').write_bytes(content); (attempt / 'TRIM.IN').write_bytes(content)
        candidates = [p for p in runtime.iterdir() if p.name.lower() == 'trim.dat']
        if len(candidates) != 1: raise ValueError('Expected one unambiguous TRIM.dat')
        candidates[0].write_bytes(events); (attempt / 'TRIM.DAT').write_bytes(events)
        (runtime / 'TRIMAUTO').write_bytes(d.archive_data(Path(case['source']), 'TRIMAUTO'))
        (runtime / 'Data/TRIM.cfg').write_bytes(d.archive_data(Path(case['source']), 'Data/TRIM.cfg'))
        timeout = min(JOB_TIMEOUT, int(deadline - time.monotonic() - 90))
        if timeout <= 0: raise RuntimeError('Cumulative deadline reached')
        scan.launch_command(['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                             sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime), '--name', case['name']],
                            runtime, timeout, environment=env, process_group=True, display_name=case['name'])
        if (runtime / 'TRIM.IN').read_bytes() != content or candidates[0].read_bytes() != events:
            raise ValueError('Executed input changed')
        ack = json.loads((runtime / 'completion-ack.json').read_text())
        if ack['status'] != 'sent' or ack['title'] != completion.TITLE: raise ValueError('Missing completion evidence')
        for name in ('normal-completion-screen.png', 'normal-completion-window-tree.txt'):
            if not (runtime / name).is_file(): raise ValueError('Missing completion capture')
        result.update(analyse(case, scan.collect_runtime_outputs(runtime)), status='COMPLETE', normal_completion_acknowledged=True)
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
        result.update(wall_seconds=time.monotonic() - started,
                      artifact_sha256={str(p): scan.sha256_file(p) for p in sorted(attempt.iterdir()) if p.is_file()})
        write_json(directory / 'result.json', result)
    return result


def publish(state, results):
    write_json(RUN / 'status.json', state); write_json(RUN / 'results.json', results)
    comparisons = depth_checks(results); write_json(RUN / 'depth_checks.json', comparisons)
    columns = ['name', 'status', 'family', 'ion', 'energy_kev', 'geometry', 'ionization_fraction',
               'stopped_ion_mean_dx_mm', 'reported_range_summary_mm', 'endpoint_count', 'wall_seconds']
    atomic_text(RUN / 'summary.csv', csv_text(results, columns))
    body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>SRIM C2c bulk-corner validation</title><style>body{font:16px system-ui;margin:30px}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>C2c: Ar/Kr/Xe interior-start corner validation — production held</h1>
<p>37 fixed 50-ion diagnostic cases: one exact Kr/F100keV replay, then all C/F/noble species
at 1keV/1MeV, 80% noble, 1 atm, two central-start depths. Replay and all low-energy cases
gate high-energy release. 18 fresh seeds are paired across depth; this is NOT 36 independent
samples, a precision certification or detector-quenching calibration.</p>
<p>Range means endpoint X minus initial X. The absolute RANGE.TXT summary is retained separately.
No primary escape, complete endpoints and zero edge-bin ionization are mandatory. Depth differences
must be at most 0.2% for ionization and 0.5% for displacement: numerical screening limits, not statistical equivalence.</p>
<p>At most 15 cores; 24/32 GiB RAM; zero scan swap; six-hour cumulative cap and three-hour job cap.
Completion requires user review; no automatic production, baseline rebuild or next stage.
<a href="plan.json">Frozen plan and forecast</a> · <a href="summary.csv">CSV</a> ·
<a href="logs/campaign.log">Log</a> · <a href="../kr_bulk_geometry_retry/review/index.html">C2b review</a></p>'''
    body += table([state], list(state))
    if results: body += table(results, columns)
    if comparisons: body += '<h2>Paired-depth checks</h2>' + table(comparisons, list(comparisons[0]))
    atomic_text(RUN / 'index.html', body)


def execute():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS or
            (group / 'memory.max').read_text().strip() != str(32 * GIB) or
            (group / 'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bulk_corner_validation.sh for enforced resources')
    used = json.loads((RUN / 'budget.json').read_text())['used_seconds'] if (RUN / 'budget.json').exists() else 0.
    if used >= BUDGET or memory_info()['MemAvailable'] < 16 * GIB: raise RuntimeError('Insufficient memory/time budget')
    if (RUN / 'completion.json').exists(): raise RuntimeError('Already complete; review instead of relaunching')
    started = time.monotonic(); deadline = started + BUDGET - used; results = []
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), phase='Preparing frozen corner validation')
    def stop(sig, frame): scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    def update():
        elapsed = used + time.monotonic() - started
        if elapsed >= BUDGET: scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(),
                     completed_cases=sum(r['status'] == 'COMPLETE' for r in results), expected_cases=37,
                     cumulative_hours=elapsed / 3600, resources=resource_sample())
        write_json(RUN / 'budget.json', dict(used_seconds=elapsed, limit_seconds=BUDGET)); publish(state, results)
    try:
        update(); plan, inputs = prepare(RUN)
        context = scan.resolve_execution_context(scan.load_config(d.CONFIG))
        source = scan.resolve_srim_root(None, scan.load_config(d.CONFIG))
        runtimes, _ = scan.prepare_runtimes(source, RUN, WORKERS, context)
        write_json(RUN / f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                          cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase in ('canary', 'high_energy'):
            state['phase'] = phase; pending = []
            for case in plan['cases']:
                if case['phase'] != phase: continue
                result = cached(case)
                if result: results.append(result)
                else: pending.append(case)
            pending.sort(key=lambda c: c['estimated_seconds'], reverse=True)
            if any(not c['passed'] for c in depth_checks(results)): raise ValueError('Cached depth gate failed')
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                available, active = list(runtimes), {}
                while pending or active:
                    while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline - time.monotonic() < JOB_TIMEOUT + 90:
                            scan.SHUTDOWN_REQUESTED.set(); break
                        case, runtime = pending.pop(0), available.pop()
                        active[pool.submit(run_case, case, inputs[case['name']], runtime,
                                           scan.runtime_execution_context(context, RUN, runtime), deadline)] = runtime
                    if not active: break
                    done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                    for future in done:
                        available.append(active.pop(future))
                        try: result = future.result()
                        except Exception:
                            scan.SHUTDOWN_REQUESTED.set(); raise
                        results.append(result)
                        print(json.dumps({k: v for k, v in result.items() if k != 'artifact_sha256'}), flush=True)
                        if result['status'] != 'COMPLETE' or any(not c['passed'] for c in depth_checks(results)):
                            scan.SHUTDOWN_REQUESTED.set()
                    update()
            if any(r['status'] == 'FAILED' for r in results) or any(not c['passed'] for c in depth_checks(results)):
                raise RuntimeError('Case or paired-depth gate failed; further release held')
            if scan.SHUTDOWN_REQUESTED.is_set(): raise InterruptedError('Stopped or insufficient cumulative budget')
            if phase == 'canary' and (len(results) != 19 or len(depth_checks(results)) != 9 or
                                     not next(r for r in results if 'replay_reference' in r).get('replay_passed')):
                raise RuntimeError('Replay and all nine low-energy pairs must pass')
            update()
        prepare(RUN)
        if len(results) != 37 or len(depth_checks(results)) != 18 or int(dict(line.split() for line in (group / 'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('Final count or OOM check failed')
        state.update(status='REVIEW_REQUIRED', phase='Corner validation complete; production held', source_unchanged=True)
        update(); write_json(RUN / 'completion.json', state); return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker', 'status'))
    parser.add_argument('--runtime', type=Path); parser.add_argument('--name'); args = parser.parse_args()
    if args.command == 'worker': completion.run(args.runtime, args.name); return 0
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN / 'status.json').exists(): print((RUN / 'status.json').read_text())
        print('Report:', RUN / 'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN)
            print(json.dumps({k: plan[k] for k in ('stage', 'total_cases', 'diagnostic_histories', 'forecast', 'budget_hours', 'design')}, indent=2)); return 0
        return execute()


if __name__ == '__main__': raise SystemExit(main())
