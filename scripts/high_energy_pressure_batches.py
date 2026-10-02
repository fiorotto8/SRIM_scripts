#!/usr/bin/env python3
"""P2b: three reserved independent batches per P2a point; stop for review."""
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
import shutil
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import review_high_energy_pressure as previous
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB

prior, b, scan = previous.prior, previous.b, previous.scan
RUN = ROOT / 'runs/noble_cf4_high_energy_pressure_batches'
UNIT = 'srim-high-energy-pressure-batches.service'
MODEL = prior.MODEL
WORKERS = 15
BUDGET = 8 * 3600
JOB_TIMEOUT = 3 * 3600


def make_input(contents, name, seed):
    if not 0 < seed < 900000001:
        raise ValueError('Invalid reserved seed')
    content, events = contents
    lines = content.decode('ascii').splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('Cascades(')) + 1
    fields = lines[index].split()
    if fields[0] != '5':
        raise ValueError('Expected unchanged mode 5')
    fields[1] = str(seed); lines[index] = ' '.join(fields)
    index = lines.index('Target material : Number of Elements, Number of Layers') + 1
    lines[index] = f'"{name}" 3 1'
    event_lines = events[:-1].decode('ascii').splitlines()
    if events[-1:] != b'\x1a' or len(event_lines[10:]) != 50:
        raise ValueError('Expected fifty unchanged events and DOS EOF')
    old_name, suffix = event_lines[7].split(' ', 1)
    event_lines[7] = f'{name} {suffix}'
    return (('\r\n'.join(lines) + '\r\n').encode('ascii'),
            ('\r\n'.join(event_lines) + '\r\n').encode('ascii') + b'\x1a')


def prepare(run):
    review, retained, _, baseline, old_plan = previous.review()
    cases, inputs = [], {}
    old_inputs = {row['name']: ((Path(row['attempt']) / 'TRIM.IN').read_bytes(),
                               (Path(row['attempt']) / 'TRIM.DAT').read_bytes()) for row in retained}
    old_cases = {case['name']: case for case in old_plan['cases']}
    used = {row['seed'] for row in baseline + retained}
    paths = {Path(__file__), ROOT / 'scripts/high_energy_pressure_batches.sh', Path(previous.__file__),
             previous.RUN / 'plan.json', previous.RUN / 'completion.json', previous.RUN / 'results.json'}
    for row in retained:
        paths.add(previous.RUN / 'cases' / row['name'] / 'result.json')
        paths.update(Path(p) for p in row['artifact_sha256'])
        for repeat in (1, 2, 3):
            name = row['point'] + f'_b{repeat}'
            seed = old_plan['reserved_independent_seeds'][row['point']][repeat]
            if seed in used:
                raise ValueError('Reserved P2b seed collision')
            used.add(seed)
            content, events = make_input(old_inputs[row['name']], name, seed)
            inputs[name] = (content, events)
            case = dict(old_cases[row['name']])
            case.update(name=name, repeat=repeat, seed=seed, phase='independent_pressure_batches',
                        input_sha256=hashlib.sha256(content).hexdigest(),
                        trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                        estimated_seconds=row['wall_seconds'], retained_batch=row['name'])
            cases.append(case)
    loads = [0.] * WORKERS
    for cost in sorted((case['estimated_seconds'] for case in cases), reverse=True):
        heapq.heapreplace(loads, loads[0] + cost)
    model = max(loads) / 3600
    forecast = dict(model_hours=model, planning_hours=1.5 * model + .5,
                    longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases) / 3600,
                    method='Measured P2a same-condition batch times, three independent batches per point, '
                           '15-worker longest-first scheduling, 50% margin plus 30 minutes. '
                           'One timing observation per condition; seed and contention variation remain uncertain.')
    if len(cases) != 54 or len(used) != 1044 or forecast['planning_hours'] > 7 or forecast['longest_estimated_batch_hours'] > 2.5:
        raise ValueError('P2b count, independence or budget gate failed')
    plan = dict(stage='P2b', model_id=MODEL, cases=cases, total_cases=54, retained_cases=18,
                completed_points_after_stage=18, independent_batches_after_stage=72,
                new_histories=2700, retained_histories=900, workers=WORKERS, budget_hours=8,
                job_timeout_seconds=JOB_TIMEOUT, forecast=forecast, previous_review=review,
                pressure_screen=old_plan['pressure_screen_after_p2b'],
                input_sha256={str(p): scan.sha256_file(p) for p in sorted(paths)},
                design='Exactly P2a reserved repeats 1/2/3 for all eighteen 1-MeV, 80%-noble points at 0.5/3 atm. '
                       'Only seed and identifying labels change; geometry, species, energy, bindings, density and events unchanged. '
                       'P2a b0 is retained by raw-audited provenance, never rerun or counted as a diagnostic.',
                gates='All frozen complete input/raw/actual-parameter/density/endpoint/containment/completion/display/source gates. '
                      'Four independent 50-ion batches per point for estimated SEM: range <=5%, ionization <=2%. '
                      'Precision flags do not trigger repeated sampling. The 36 Bonferroni approximate Welch log-batch '
                      'pressure intervals use the already frozen specification; all outcomes reported.',
                next='Stop and review P2b before the 315-point M3 mixture expansion. An unresolved numerical or '
                     'material pressure feature requires a bounded diagnostic/independent holdout, not an automatic expansion.')
    path = run / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen P2b plan changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs, retained, baseline


def pressure_checks(rows, baseline, spec):
    from scipy.stats import t
    if spec != prior.PRESSURE_SPEC:
        raise ValueError('Prespecified P2b pressure family changed')
    expected = {(f, ion, 1000., 80, p) for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f) for p in (.5, 3.)}
    selected = [row for row in baseline if b.key(row)[:4] in {(f, ion, 1000., 80) for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)}
                and row['pressure_atm'] == 1.] + rows
    if len({row['seed'] for row in selected}) != len(selected):
        raise ValueError('Duplicate pressure-screen seed')
    groups = {}
    for row in selected:
        if row['status'] != 'COMPLETE' or row['model_id'] != MODEL or row['ions_confirmed'] != 50:
            raise ValueError('Invalid pressure-screen batch')
        groups.setdefault(b.key(row), []).append(row)
    if set(groups) != expected | {(f, ion, 1000., 80, 1.) for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)}:
        raise ValueError('Unexpected pressure-screen points')
    if any(len(rr) != 4 or {r['repeat'] for r in rr} != set(range(4)) for rr in groups.values()):
        raise ValueError('Pressure screen requires four independent batches per condition')
    result = []
    for f, ion, energy, noble, pressure in sorted(expected):
        reference, observed = groups[f, ion, energy, noble, 1.], groups[f, ion, energy, noble, pressure]
        for field, margin in [('ionization_fraction', spec['ionization_ratio_margin']),
                              ('stopped_ion_mean_dx_mm', spec['pressure_scaled_range_ratio_margin'])]:
            def logs(batch):
                values = [r[field] * (r['pressure_atm'] if field == 'stopped_ion_mean_dx_mm' else 1.) for r in batch]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError('Invalid pressure observable')
                return [math.log(v) for v in values]
            a, c = logs(reference), logs(observed)
            va, vc = statistics.variance(a) / 4, statistics.variance(c) / 4
            delta = statistics.mean(c) - statistics.mean(a)
            item = dict(family=f, ion=ion, energy_kev=energy, pressure_atm=pressure, metric=field,
                        ratio=math.exp(delta), margin_low=margin[0], margin_high=margin[1],
                        classification='inconclusive_zero_variance', comparisons=36, reference_batches=4, pressure_batches=4)
            if va + vc > 0:
                df = (va + vc) ** 2 / (va * va / 3 + vc * vc / 3)
                half = float(t.ppf(1 - spec['familywise_alpha'] / (2 * spec['comparisons']), df)) * math.sqrt(va + vc)
                low, high = math.exp(delta - half), math.exp(delta + half)
                label = 'within_working_margin' if low >= margin[0] and high <= margin[1] else 'followup' if high < margin[0] or low > margin[1] else 'inconclusive'
                item.update(ratio_low=low, ratio_high=high, welch_df=df, classification=label)
            result.append(item)
    if len(result) != 36:
        raise ValueError('Pressure family count changed')
    return result


def run_case(case, contents, runtime, context, deadline):
    (runtime / '.completion-probes.json').unlink(missing_ok=True)
    row = b.run_case(case, contents, runtime, context, deadline)
    metadata = runtime / '.completion-probes.json'
    if metadata.exists():
        target = Path(row['attempt']) / metadata.name; shutil.copy2(metadata, target)
        row['artifact_sha256'][str(target)] = scan.sha256_file(target)
        row['completion_monitor'] = json.loads(target.read_text())
    if row['status'] == 'COMPLETE':
        try:
            row.update(prior.check_density(case, scan.collect_runtime_outputs(runtime)))
            if 'completion_monitor' not in row:
                raise ValueError('Missing completion-monitor evidence')
        except Exception as exc:
            row.update(status='FAILED', error=str(exc))
    write_json(RUN / 'cases' / case['name'] / 'result.json', row)
    return row


def publish(state, rows, retained):
    combined = retained + rows; points = b.p.point_summary(combined)
    state.update(completed_new_batches=sum(r['status'] == 'COMPLETE' for r in rows), expected_new_batches=54,
                 retained_batches=len(retained), points_complete=sum(p['complete'] for p in points), expected_points=18,
                 points_meeting_precision=sum(p['precision_met'] for p in points),
                 completed_points_flagged=sum(p['complete'] and not p['precision_met'] for p in points))
    for name, data in [('status', state), ('results', rows), ('points', points)]:
        write_json(RUN / (name + '.json'), data)
    if points:
        atomic_text(RUN / 'points.csv', csv_text(points, list(points[0])))
    columns = ['name', 'status', 'family', 'ion', 'energy_kev', 'pressure_atm', 'repeat', 'seed', 'ions_confirmed',
               'stopped_ion_mean_dx_mm', 'ionization_fraction', 'wall_seconds', 'attempt']
    atomic_text(RUN / 'batches.csv', csv_text(combined, columns))
    body = '<html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>P2b pressure batches</title>'
    body += '<h1>P2b: independent 1-MeV pressure batches</h1><p>Ar/Kr/Xe80/CF4-20, C/F/noble, 0.5/3 atm. '
    body += '18 audited P2a batches retained plus 54 fresh reserved batches: 200 ions/point. '
    body += '15-core maximum, 24/32 GiB RAM, zero scan swap, eight-hour cap. No automatic M3 launch.</p>'
    body += '<p>Range is endpoint minus start; ionization fraction is not detector-calibrated quenching. '
    body += 'Fixed samples: estimated SEM targets 5% range and 2% ionization; flags remain visible.</p>'
    body += '<p><a href="points.csv">Points and SEM CSV</a> · <a href="batches.csv">Batches CSV</a> · '
    body += '<a href="plan.json">Frozen plan</a> · <a href="../noble_cf4_high_energy_pressure/review/index.html">P2a audit</a></p>'
    body += table([state], list(state))
    if points:
        body += table(points, list(points[0]))
    if (RUN / 'pressure_checks.json').exists():
        body += '<p><a href="pressure_checks.csv">36 prespecified pressure intervals</a></p>'
    atomic_text(RUN / 'index.html', body)


def execute():
    group = Path(resource_sample()['cgroup'])
    if group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS or (group / 'memory.max').read_text().strip() != str(32 * GIB) or (group / 'memory.swap.max').read_text().strip() != '0':
        raise RuntimeError('Use the resource-limited P2b launcher')
    used = json.loads((RUN / 'budget.json').read_text())['used_seconds'] if (RUN / 'budget.json').exists() else 0.
    if used >= BUDGET or memory_info()['MemAvailable'] < 16 * GIB:
        raise RuntimeError('Insufficient time/RAM allowance')
    if (RUN / 'completion.json').exists():
        raise RuntimeError('Already complete; review instead of relaunching')
    started = time.monotonic(); deadline = started + BUDGET - used; rows, retained = [], []
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(),
                 phase='Preparing audited pressure batches', model_id=MODEL)
    def stop(sig, frame):
        scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    def update():
        elapsed = used + time.monotonic() - started
        if elapsed >= BUDGET:
            scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(), cumulative_hours=elapsed / 3600, resources=resource_sample())
        write_json(RUN / 'budget.json', dict(used_seconds=elapsed, limit_seconds=BUDGET)); publish(state, rows, retained)
    try:
        update(); plan, inputs, retained, baseline = prepare(RUN)
        config = scan.load_config(b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, WORKERS, context)
        write_json(RUN / f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                        cpu_affinity=sorted(os.sched_getaffinity(0))))
        pending = []
        for case in plan['cases']:
            cached = b.base.cached(case)
            if cached:
                prior.audit_case(case, cached, inputs[case['name']]); rows.append(cached)
            else:
                pending.append(case)
        pending.sort(key=lambda c: c['estimated_seconds'], reverse=True); state['phase'] = 'independent_pressure_batches'
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            available, active = list(runtimes), {}
            while pending or active:
                while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                    if deadline - time.monotonic() < JOB_TIMEOUT + 90:
                        scan.SHUTDOWN_REQUESTED.set(); break
                    case, runtime = pending.pop(0), available.pop()
                    active[pool.submit(run_case, case, inputs[case['name']], runtime,
                                       scan.runtime_execution_context(context, RUN, runtime), deadline)] = runtime
                if not active:
                    break
                done, _ = wait(active, timeout=5, return_when=FIRST_COMPLETED)
                for future in done:
                    available.append(active.pop(future))
                    try:
                        row = future.result()
                    except Exception:
                        scan.SHUTDOWN_REQUESTED.set(); raise
                    rows.append(row); print(json.dumps({k: v for k, v in row.items() if k != 'artifact_sha256'}), flush=True)
                    if row['status'] != 'COMPLETE':
                        scan.SHUTDOWN_REQUESTED.set()
                update()
        if any(row['status'] == 'FAILED' for row in rows):
            raise RuntimeError('Integrity failure; mixture expansion held')
        if scan.SHUTDOWN_REQUESTED.is_set():
            raise InterruptedError('Stopped or insufficient cumulative budget')
        prepare(RUN); indexed = {row['name']: row for row in rows}
        if len(rows) != 54 or len(indexed) != 54:
            raise ValueError('Incomplete P2b cohort')
        for case in plan['cases']:
            prior.audit_case(case, indexed[case['name']], inputs[case['name']])
        points = b.p.point_summary(retained + rows)
        if len(points) != 18 or not all(p['complete'] for p in points):
            raise ValueError('Incomplete independent pressure points')
        checks = pressure_checks(retained + rows, baseline, plan['pressure_screen'])
        write_json(RUN / 'pressure_checks.json', checks); atomic_text(RUN / 'pressure_checks.csv', csv_text(checks, sorted({k for r in checks for k in r})))
        if int(dict(line.split() for line in (group / 'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('OOM observed')
        state.update(status='REVIEW_REQUIRED', phase='P2b complete; review pressure and precision before M3', source_unchanged=True,
                     combined_independent_batches=72, combined_histories=3600,
                     longest_batch_seconds=max(row['wall_seconds'] for row in rows))
        update(); write_json(RUN / 'completion.json', state); return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN / 'status.json').exists():
            print((RUN / 'status.json').read_text())
        print('Report:', RUN / 'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(RUN); print(json.dumps({k: plan[k] for k in ('stage', 'total_cases', 'new_histories', 'forecast')}, indent=2)); return 0
        b.RUN = RUN; b.base.RUN = RUN; scan.launch_command = prior.r.launch
        return execute()


if __name__ == '__main__':
    raise SystemExit(main())
