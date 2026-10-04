#!/usr/bin/env python3
"""P4: fixed direct 80%-noble pressure/energy completion, manual next-stage gate."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
from datetime import datetime
import csv
import fcntl
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import signal
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import review_mixtures as previous
from scripts import campaign_schedule as schedule
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB
from srim_gas_sweep import load_elements

p, b, scan = previous.p, previous.b, previous.scan
gate = previous.prior.gate
RUN = ROOT/'runs/noble_cf4_pressure_grid'
UNIT = 'srim-pressure-grid.service'
MODEL = p.MODEL
WORKERS, BUDGET, JOB_TIMEOUT = 15, 24*3600, 3*3600
PHASES = ('low_pressure_gate', 'high_pressure_gate', 'pressure_production')
PRESSURES = (.5, 1., 1.5, 2., 2.5, 3.)
original_audit = previous.prior.original_audit


def selection(existing):
    full = {(f, ion, e, 80, pressure) for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)
            for e in p.ENERGIES for pressure in PRESSURES}
    return full-set(existing)


def make_input(original, condition, name, seed, z, energy, width):
    if condition.noble_percent != 80 or condition.pressure_atm not in PRESSURES or width <= 0 or not math.isfinite(width):
        raise ValueError('Unexpected P4 condition or geometry')
    lines = original.decode('ascii').splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('1 "'))
    fractions = ' '.join(f'{condition.atomic_fractions[s]:.17g}' for s in ('C', 'F', condition.family))
    lines[index] = f'1 "{condition.condition_id}" {width/2:.17g} {condition.density_g_cm3:.17g} {fractions}'
    return b.make_input(('\r\n'.join(lines)+'\r\n').encode('ascii'), name, seed, z, energy, 2)


def seed_registry(run, rows):
    saved = run/'plan.json'
    paths = ([Path(x) for x in json.loads(saved.read_text())['seed_registry_paths']] if saved.exists()
             else sorted(x for x in (ROOT/'runs').glob('*/plan.json') if x.parent != RUN))
    seeds = {r['seed'] for r in rows}
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('seed', 'random_seed') and isinstance(child, int) and child > 0:
                    seeds.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for path in paths:
        visit(json.loads(path.read_text()))
    with schedule.TIMINGS.open(newline='') as stream:
        legacy = list(csv.DictReader(stream))
    if len(legacy) != 1841:
        raise ValueError('Legacy seed reference changed')
    seeds.update(int(row['random_seed']) for row in legacy)
    return seeds, paths


def batch_cost(key, means):
    f, ion, e, _, pressure = key
    if e in (1., 10., 100., 1000.):
        return max(means[f, ion, e, 80, q] for q in (.5, 1., 3.))
    factor = max(1., *(means[f, ion, energy, 80, q]/means[f, ion, energy, 80, 1.]
                      for energy in (1., 10., 100., 1000.) for q in (.5, 3.)))
    return means[f, ion, e, 80, 1.]*factor


def forecast(cases):
    phases = {}
    for phase in PHASES:
        loads = [0.]*WORKERS
        for cost in sorted((c['estimated_seconds'] for c in cases if c['phase'] == phase), reverse=True):
            heapq.heapreplace(loads, loads[0]+cost)
        phases[phase] = max(loads)/3600
    model = sum(phases.values())
    return dict(model_hours=model, planning_hours=1.5*model+.5, phase_model_hours=phases,
                longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases)/3600,
                method='Maximum four-batch mean over measured0.5/1/3atm at the same energy where available. '
                       'For3/30/300keV multiply measured1atm mean by the maximum observed pressure runtime ratio '
                       'over1/10/100/1000keV for that family/recoil,never belowone. Three sequential gates,15workers, '
                       'longest-first,50%margin plus30minutes. Runtime planning only; no scaled physics results.')


def prepare(run):
    with previous.prior.historical_audits():
        review, retained, points, timing, _ = previous.review()
    refs = {}
    for row in sorted(retained, key=lambda x: x['repeat']):
        refs.setdefault(b.key(row), row)
    keys = selection(refs)
    if len(keys) != 243 or any(k[4] == 1. for k in keys):
        raise ValueError('P4 grid accounting changed')
    config = scan.load_config(b.base.d.CONFIG); srim = scan.resolve_srim_root(None, config)
    elements, _ = load_elements(srim/'Data/ATOMDATA')
    conditions = {(c.family, c.pressure_atm): c for c in scan.generate_conditions(
        replace(config, noble_percent=(80,), pressures_atm=PRESSURES), elements)}
    means = {b.key(row): row['mean_seconds'] for row in timing}
    forbidden, seed_paths = seed_registry(run, retained); used = set(forbidden)
    cases, inputs = [], {}
    old_plan = json.loads((previous.RUN/'plan.json').read_text())
    paths = {Path(__file__), ROOT/'scripts/pressure_grid.sh', Path(previous.__file__), schedule.TIMINGS,
             previous.RUN/'plan.json', previous.RUN/'completion.json', previous.RUN/'status.json',
             previous.RUN/'results.json', previous.RUN/'points.json', srim/'Data/ATOMDATA'}
    paths.update(seed_paths)
    for row in retained:
        paths.update(Path(x) for x in row['artifact_sha256'])
    for key in sorted(keys):
        family, ion, energy, _, pressure = key
        source = refs[family, ion, energy, 80, 1.]
        original = (Path(source['attempt'])/'TRIM.IN').read_bytes()
        condition = conditions[family, pressure]
        width = source['width_angstrom']/pressure
        # Retain at least the largest measured areal target width, rather than
        # shrinking a previously validated containment envelope.
        width = max(width, *(row['width_angstrom']*row['pressure_atm']/pressure
                             for k, row in refs.items() if k[:4] == key[:4]))
        point = b.name_for(key)
        for repeat in range(4):
            name = point+f'_b{repeat}'; seed = scan.seed_for(MODEL+'-P4-pressure-grid-'+point, repeat)
            if seed in used:
                raise ValueError('New pressure seed collision; do not replace silently')
            used.add(seed)
            content, events, origin, actual_width = make_input(original, condition, name, seed, source['ion_z'], energy, width)
            low = energy == 1. or (energy == 3. and pressure in (.5, 3.))
            phase = 'low_pressure_gate' if repeat == 0 and low else 'high_pressure_gate' if repeat == 0 and energy == 1000. else 'pressure_production'
            case = dict(name=name, point=point, repeat=repeat, phase=phase, diagnostic=False, model_id=MODEL,
                        family=family, ion=ion, ion_z=source['ion_z'], energy_kev=energy, noble_percent=80,
                        pressure_atm=pressure, geometry='interior', ions=50, seed=seed, source=source['source'],
                        template_input=str(Path(source['attempt'])/'TRIM.IN'), condition_id=condition.condition_id,
                        density_g_cm3=condition.density_g_cm3, atomic_fractions=condition.atomic_fractions,
                        mass_fractions=condition.mass_fractions, origin_angstrom=origin, width_angstrom=actual_width,
                        expected=source['expected'], input_sha256=hashlib.sha256(content).hexdigest(),
                        trim_dat_sha256=hashlib.sha256(events).hexdigest(), estimated_seconds=batch_cost(key, means))
            p.validate_input(case, (content, events), condition)
            cases.append(case); inputs[name] = content, events
    estimate = forecast(cases)
    if (len(cases) != 972 or len(used-forbidden) != 972 or
            [sum(c['phase'] == phase for c in cases) for phase in PHASES] != [45, 27, 900]
            or estimate['planning_hours']+(JOB_TIMEOUT+90)/3600 > 24
            or estimate['longest_estimated_batch_hours'] > 2.5):
        raise ValueError('P4 count/independence/time budget gate failed: '+json.dumps(estimate))
    manifest = {**old_plan['input_sha256'], **{str(path): p.history.actual_sha256(path) for path in sorted(paths)}}
    plan = dict(stage='P4', model_id=MODEL, cases=cases, total_cases=972, new_points=243, new_histories=48600,
                retained_points=576, retained_batches=2304, combined_points_after_stage=819,
                workers=WORKERS, budget_hours=24, job_timeout_seconds=JOB_TIMEOUT,
                seed_registry_paths=[str(x) for x in seed_paths], forbidden_seed_count=len(forbidden),
                forbidden_seed_sha256=hashlib.sha256(json.dumps(sorted(forbidden)).encode()).hexdigest(),
                forecast=estimate, previous_review=review, input_sha256=manifest,
                pressure_screen=dict(comparisons=630, familywise_alpha=.05, ionization_margin=[.95, 1.05],
                                     pressure_scaled_displacement_margin=[.9, 1.1],
                                     method='Approximate Bonferroni630 Welch intervals for mean log-batch differences, '
                                            '9family/recoils x7energies x5non1atm pressures x2observables. '
                                            'Four batches percondition; geometric ratios. Existing anchors visible; '
                                            'screening,not independent confirmation or global equivalence.'),
                gates='45low-energy b0 production canaries,then27intermediate-pressure1MeV b0 canaries,then900otherbatches. '
                      'Exact executed inputs,seeds,bindings,composition,density,all50contained endpoints,noescape/edge signal, '
                      'normal completion andvalidated authenticated-noreset displays. New independent seeds disjoint '
                      'from predecessor plans,2304bulk histories seeds and1841legacy surface records. '
                      'Fixed4x50ions/point,5%range/2%ionization SEM goals;flags retained,noadaptive topups. '
                      '819completecombinedpoints,378at80%noble. Manualcallback beforeG5; noautomaticnextwave.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen P4 plan changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs, retained


def audit_case(case, row, contents):
    original_audit(case, row, contents)
    if (not Path(row['attempt']).is_relative_to(RUN/'cases') or
            row['display_startup'].get('reset_on_last_client') is not False or
            row['display_startup'].get('helper') != str(Path(gate.fixed_display.__file__))):
        raise ValueError('Missing validated noreset lifecycle')


def pressure_checks(rows, spec):
    from scipy.stats import t
    if (spec['comparisons'] != 630 or spec['familywise_alpha'] != .05 or spec['ionization_margin'] != [.95, 1.05]
            or spec['pressure_scaled_displacement_margin'] != [.9, 1.1]):
        raise ValueError('Prespecified pressure screen changed')
    groups = {}
    seen = set()
    for row in rows:
        if row['noble_percent'] == 80:
            if row['status'] != 'COMPLETE' or row['ions_confirmed'] != 50 or row['model_id'] != MODEL or row['seed'] in seen:
                raise ValueError('Invalid or duplicate pressure-screen batch')
            seen.add(row['seed'])
            groups.setdefault(b.key(row), []).append(row)
    if len(groups) != 378 or any(len(v) != 4 or {r['repeat'] for r in v} != set(range(4)) for v in groups.values()):
        raise ValueError('Pressure screen requires378four-batch points')
    checks = []
    for key, observed in sorted(groups.items()):
        if key[4] == 1.:
            continue
        reference = groups[(*key[:4], 1.)]
        for metric, margin in [('ionization_fraction', spec['ionization_margin']),
                               ('stopped_ion_mean_dx_mm', spec['pressure_scaled_displacement_margin'])]:
            def logs(batch):
                values = [r[metric]*(r['pressure_atm'] if metric == 'stopped_ion_mean_dx_mm' else 1.) for r in batch]
                if not all(math.isfinite(v) and v > 0 for v in values):
                    raise ValueError('Invalid pressure observable')
                return [math.log(v) for v in values]
            a, c = logs(reference), logs(observed)
            va, vc = statistics.variance(a)/4, statistics.variance(c)/4
            delta = statistics.mean(c)-statistics.mean(a)
            item = dict(family=key[0], ion=key[1], energy_kev=key[2], pressure_atm=key[4], metric=metric,
                        ratio=math.exp(delta), margin_low=margin[0], margin_high=margin[1],
                        classification='inconclusive_zero_variance')
            if va+vc > 0:
                df = (va+vc)**2/(va*va/3+vc*vc/3)
                half = float(t.ppf(1-spec['familywise_alpha']/(2*spec['comparisons']), df))*math.sqrt(va+vc)
                low, high = math.exp(delta-half), math.exp(delta+half)
                label = 'within_working_margin' if low >= margin[0] and high <= margin[1] else 'followup' if high < margin[0] or low > margin[1] else 'inconclusive'
                item.update(ratio_low=low, ratio_high=high, welch_df=df, classification=label)
            checks.append(item)
    if len(checks) != spec['comparisons']:
        raise ValueError('Pressure screen family changed')
    return checks


def publish(state, rows, retained):
    points = b.p.point_summary(retained+rows); new_points = b.p.point_summary(rows)
    state.update(completed_batches=sum(r['status'] == 'COMPLETE' for r in rows), expected_batches=972,
                 new_points_complete=sum(x['complete'] for x in new_points), expected_new_points=243,
                 combined_points_complete=sum(x['complete'] for x in points),
                 completed_precision_flags=sum(x['complete'] and not x['precision_met'] for x in points))
    for name, value in [('status', state), ('results', rows), ('points', points)]:
        write_json(RUN/(name+'.json'), value)
    if points:
        atomic_text(RUN/'points.csv', csv_text(points, sorted({k for x in points for k in x})))
    columns = ['name', 'status', 'family', 'ion', 'energy_kev', 'noble_percent', 'pressure_atm', 'repeat', 'seed',
               'stopped_ion_mean_dx_mm', 'ionization_fraction', 'wall_seconds', 'attempt']
    atomic_text(RUN/'batches.csv', csv_text(retained+rows, columns))
    body = '<html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><h1>P4: direct pressure/energy grid</h1>'
    body += '<p>Ar/Kr/Xe80/CF4-20; C/F/noble recoils,0.5–3atm,1–1000keV.243newpoints,972fixed independent50-ion batches. '
    body += '45low-energy then27high-energy canaries gate900remaining batches.15cores,24/32GiB RAM,zero scan swap,24hcap. '
    body += 'Range = endpoint minus start; ionization fraction is not calibrated detector quenching. '
    body += '99preceding precision flags retained; noautomatic topups orG5.</p><p><a href="points.csv">Combined points/SEM CSV</a> · '
    body += '<a href="batches.csv">Batch CSV</a> · <a href="plan.json">Frozen plan</a> · '
    body += '<a href="../noble_cf4_mixtures_1atm_noreset/review/index.html">Mixture audit and PDF</a></p>'
    if (RUN/'pressure.pdf').exists():
        body += '<p><a href="pressure.pdf">Pressure plots</a> · <a href="pressure_checks.csv">630 pressure intervals</a></p>'
    atomic_text(RUN/'index.html', body+table([state], list(state)))


def plots(points):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    with PdfPages(RUN/'pressure.pdf') as pdf:
        for family in ('Ar', 'Kr', 'Xe'):
            for ion in ('C', 'F', family):
                fig, axes = plt.subplots(1, 2, figsize=(12, 5))
                for energy in p.ENERGIES:
                    rr = sorted((x for x in points if x['family'] == family and x['ion'] == ion
                                 and x['noble_percent'] == 80 and x['energy_kev'] == energy), key=lambda x: x['pressure_atm'])
                    for ax, metric in zip(axes, ('projected_range_mm', 'ionization_fraction')):
                        scales = [x['pressure_atm'] if metric == 'projected_range_mm' else 1 for x in rr]
                        ax.errorbar([x['pressure_atm'] for x in rr], [x[metric]*s for x, s in zip(rr, scales)],
                                    yerr=[x[metric+'_sem']*s for x, s in zip(rr, scales)], marker='.', capsize=2, label=f'{energy:g}keV')
                        ax.set_xlabel('Pressure [atm]'); ax.grid(True, alpha=.3); ax.legend(fontsize=7)
                axes[0].set_ylabel('Pressure × displacement [atm mm]'); axes[0].set_yscale('log')
                axes[1].set_ylabel('SRIM ionization fraction')
                fig.suptitle(f'{family}80/CF4-20, {ion} recoil; batch SEM bars, not simultaneous intervals')
                fig.tight_layout(); pdf.savefig(fig); plt.close(fig)


def execute():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS or
            (group/'memory.max').read_text().strip() != str(32*GIB) or (group/'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bounded P4 launcher')
    used = json.loads((RUN/'budget.json').read_text())['used_seconds'] if (RUN/'budget.json').exists() else 0.
    if used >= BUDGET or memory_info()['MemAvailable'] < 16*GIB or (RUN/'completion.json').exists():
        raise RuntimeError('Insufficient time/RAM or already completed')
    started = time.monotonic(); deadline = started+BUDGET-used; rows, retained = [], []
    state = dict(status='RUNNING', stage='P4', phase='preparing_audited_pressure_grid', started_at=datetime.now().astimezone().isoformat())
    def stop(sig, frame):
        scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop)
    def update():
        elapsed = used+time.monotonic()-started
        if elapsed >= BUDGET:
            scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(), cumulative_hours=elapsed/3600, resources=resource_sample())
        write_json(RUN/'budget.json', dict(used_seconds=elapsed, limit_seconds=BUDGET)); publish(state, rows, retained)
    try:
        update(); plan, inputs, retained = prepare(RUN)
        config = scan.load_config(b.base.d.CONFIG); context = scan.resolve_execution_context(config)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, config), RUN, WORKERS, context)
        write_json(RUN/f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                       cpu_affinity=sorted(os.sched_getaffinity(0))))
        cached_names = set()
        for case in plan['cases']:
            row = b.base.cached(case)
            if row:
                audit_case(case, row, inputs[case['name']]); rows.append(row); cached_names.add(case['name'])
        for phase in PHASES:
            pending = sorted((c for c in plan['cases'] if c['phase'] == phase and c['name'] not in cached_names),
                             key=lambda c: c['estimated_seconds'], reverse=True)
            state['phase'] = phase; update()
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                available, active = list(runtimes), {}
                while pending or active:
                    while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline-time.monotonic() < JOB_TIMEOUT+90:
                            scan.SHUTDOWN_REQUESTED.set(); break
                        case, runtime = pending.pop(0), available.pop()
                        active[pool.submit(p.run_case, case, inputs[case['name']], runtime,
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
                raise RuntimeError('P4 integrity failure; hold later phases')
            if scan.SHUTDOWN_REQUESTED.is_set():
                raise InterruptedError('Stopped or insufficient cumulative budget')
            if not {c['name'] for c in plan['cases'] if c['phase'] == phase} <= {r['name'] for r in rows if r['status'] == 'COMPLETE'}:
                raise ValueError('Incomplete P4 phase gate')
            state[phase+'_passed'] = True; update()
        prepare(RUN); indexed = {r['name']: r for r in rows}
        if len(rows) != 972 or len(indexed) != 972:
            raise ValueError('Incomplete P4 cohort')
        for case in plan['cases']:
            audit_case(case, indexed[case['name']], inputs[case['name']])
        points = b.p.point_summary(retained+rows)
        if len(points) != 819 or not all(x['complete'] for x in points) or len({r['seed'] for r in retained+rows}) != 3276:
            raise ValueError('Combined P4 point/seed accounting changed')
        checks = pressure_checks(retained+rows, plan['pressure_screen'])
        write_json(RUN/'pressure_checks.json', checks)
        atomic_text(RUN/'pressure_checks.csv', csv_text(checks, sorted({k for x in checks for k in x})))
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('OOM observed')
        plots(points)
        state.update(status='REVIEW_REQUIRED', phase='P4 complete; review pressure, precision and timings before G5',
                     source_unchanged=True, combined_independent_batches=3276, combined_histories=163800,
                     pressure_followup_intervals=sum(x['classification'] == 'followup' for x in checks),
                     longest_batch_seconds=max(r['wall_seconds'] for r in rows))
        update(); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(RUN); print(json.dumps({k: plan[k] for k in ('stage', 'total_cases', 'new_points', 'forecast')}, indent=2)); return 0
        p.RUN, b.RUN, b.base.RUN = RUN, RUN, RUN
        p.check_composition, p.audit_case, scan.launch_command = gate.retry.check_composition, audit_case, gate.launch
        return execute()


if __name__ == '__main__':
    raise SystemExit(main())
