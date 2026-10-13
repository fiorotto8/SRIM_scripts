#!/usr/bin/env python3
"""G5-85A: fixed direct 85%-noble pressure/energy wave; manual next-wave gate."""
import argparse
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
from scripts import review_mixture_pressure_75b as previous
from scripts import campaign_schedule as schedule
from scripts import mixture_pressure_65_ten as monitor
from scripts import mixture_pressure_65_isolated as isolated
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB
from srim_gas_sweep import load_elements

p, b, scan = previous.p, previous.b, previous.scan
gate = previous.r.old.gate
RUN = ROOT/'runs/noble_cf4_mixture_pressure_85a'
UNIT = 'srim-mixture-pressure-85a.service'
MODEL = p.MODEL
WORKERS, BUDGET, JOB_TIMEOUT = 10, 24*3600, 3*3600
PHASES = ('low_pressure_gate', 'high_pressure_gate', 'pressure_production')
PRESSURES = (.5, 1., 1.5, 2.)
original_audit = previous.r.old.original_audit


def selection(existing):
    full = {(f, ion, e, 85, pressure) for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)
            for e in p.ENERGIES for pressure in PRESSURES}
    return full-set(existing)


def make_input(original, condition, name, seed, z, energy, width):
    if condition.noble_percent != 85 or condition.pressure_atm not in PRESSURES or width <= 0 or not math.isfinite(width):
        raise ValueError('Unexpected G5-85A condition or geometry')
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
    f, ion, e, noble, pressure = key
    if noble != 85 or pressure == 1.:
        raise ValueError('Unexpected G5-85A timing condition')
    return means[f, ion, e, noble, 1.]*max(
        1., *(means[f, ion, e, anchor, pressure]/means[f, ion, e, anchor, 1.] for anchor in (60, 65, 70, 75, 80)))


def forecast(cases):
    phases = {}
    for phase in PHASES:
        loads = [0.]*WORKERS
        for cost in sorted((c['estimated_seconds'] for c in cases if c['phase'] == phase), reverse=True):
            heapq.heapreplace(loads, loads[0]+cost)
        phases[phase] = max(loads)/3600
    model = sum(phases.values())
    return dict(model_hours=model, planning_hours=1.5*model+.5+len(cases)/3600, phase_model_hours=phases,
                longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases)/3600,
                method='Measured85%-noble1atm four-batch mean times maximum measured60/65/70/75/80%-noble pressure/1atm mean ratio '
                       'at the same family/recoil/energy, ratio floored atone. Three sequential gates,10workers, '
                       'longest-first,50%margin plus30minutes. Runtime planning only; no scaled physics results.')


def prepare(run):
    review, retained, points, timing, _, review_manifest = previous.review()
    refs = {}
    for row in sorted(retained, key=lambda x: x['repeat']):
        refs.setdefault(b.key(row), row)
    keys = selection(refs)
    if len(keys) != 189 or any(k[4] == 1. for k in keys):
        raise ValueError('G5-85A grid accounting changed')
    config = scan.load_config(b.base.d.CONFIG); srim = scan.resolve_srim_root(None, config)
    elements, _ = load_elements(srim/'Data/ATOMDATA')
    conditions = {(c.family, c.pressure_atm): c for c in scan.generate_conditions(
        replace(config, noble_percent=(85,), pressures_atm=PRESSURES), elements)}
    means = {b.key(row): row['mean_seconds'] for row in timing}
    forbidden, seed_paths = seed_registry(run, retained); used = set(forbidden)
    cases, inputs = [], {}
    old_plan = json.loads((previous.RUN/'plan.json').read_text())
    paths = {Path(__file__), ROOT/'scripts/mixture_pressure_85a.sh', Path(previous.__file__), schedule.TIMINGS,
             previous.RUN/'plan.json', previous.RUN/'completion.json', previous.RUN/'status.json',
             previous.RUN/'results.json', previous.RUN/'points.json', previous.RUN/'pressure_checks.json',
             srim/'Data/ATOMDATA'}
    paths.update(seed_paths)
    for row in retained:
        paths.update(Path(x) for x in row['artifact_sha256'])
    for key in sorted(keys):
        family, ion, energy, _, pressure = key
        source = refs[family, ion, energy, 85, 1.]
        original = (Path(source['attempt'])/'TRIM.IN').read_bytes()
        condition = conditions[family, pressure]
        width = source['width_angstrom']/pressure
        # Retain at least the largest measured areal target width, rather than
        # shrinking a previously validated containment envelope.
        width = max(width, *(row['width_angstrom']*row['pressure_atm']/pressure
                             for k, row in refs.items() if k[:3] == key[:3]))
        point = b.name_for(key)
        for repeat in range(4):
            name = point+f'_b{repeat}'; seed = scan.seed_for(MODEL+'-G5-85A-pressure-grid-'+point, repeat)
            if seed in used:
                raise ValueError('New pressure seed collision; do not replace silently')
            used.add(seed)
            content, events, origin, actual_width = make_input(original, condition, name, seed, source['ion_z'], energy, width)
            low = energy == 1.
            phase = 'low_pressure_gate' if repeat == 0 and low else 'high_pressure_gate' if repeat == 0 and energy == 1000. else 'pressure_production'
            case = dict(name=name, point=point, repeat=repeat, phase=phase, diagnostic=False, model_id=MODEL,
                        family=family, ion=ion, ion_z=source['ion_z'], energy_kev=energy, noble_percent=85,
                        pressure_atm=pressure, geometry='interior', ions=50, seed=seed, source=source['source'],
                        template_input=str(Path(source['attempt'])/'TRIM.IN'), condition_id=condition.condition_id,
                        density_g_cm3=condition.density_g_cm3, atomic_fractions=condition.atomic_fractions,
                        mass_fractions=condition.mass_fractions, origin_angstrom=origin, width_angstrom=actual_width,
                        expected=source['expected'], input_sha256=hashlib.sha256(content).hexdigest(),
                        trim_dat_sha256=hashlib.sha256(events).hexdigest(), estimated_seconds=batch_cost(key, means))
            p.validate_input(case, (content, events), condition)
            cases.append(case); inputs[name] = content, events
    estimate = forecast(cases)
    if (len(cases) != 756 or len(used-forbidden) != 756 or
            [sum(c['phase'] == phase for c in cases) for phase in PHASES] != [27, 27, 702]
            or estimate['planning_hours']+(JOB_TIMEOUT+90)/3600 > 24
            or estimate['longest_estimated_batch_hours'] > 2.5):
        raise ValueError('G5-85A count/independence/time budget gate failed: '+json.dumps(estimate))
    manifest = {**review_manifest, **old_plan['input_sha256'], **{str(path): p.history.actual_sha256(path) for path in sorted(paths)}}
    plan = dict(inherited_used_seconds=0., retained_original_names=[], remaining_batches=756,
                thermal_policy=dict(monitor.c.read(previous.RUN,'plan.json')['thermal_policy']),
                prior_wave_cumulative_seconds=review['cumulative_wave_hours']*3600,
                stage='G5-85A', model_id=MODEL, cases=cases, total_cases=756, new_points=189, new_histories=37800,
                retained_points=2079, retained_batches=8316, combined_points_after_stage=2268,
                workers=WORKERS, budget_hours=24, job_timeout_seconds=JOB_TIMEOUT,
                seed_registry_paths=[str(x) for x in seed_paths], forbidden_seed_count=len(forbidden),
                forbidden_seed_sha256=hashlib.sha256(json.dumps(sorted(forbidden)).encode()).hexdigest(),
                forecast=estimate, previous_review=review, input_sha256=manifest,
                pressure_screen=dict(noble_percent=85, comparisons=630, observed_comparisons=378, familywise_alpha=.05, ionization_margin=[.95, 1.05],
                                     pressure_scaled_displacement_margin=[.9, 1.1],
                                     method='Approximate Bonferroni630 Welch intervals for mean log-batch differences, '
                                            'Full85%family:630comparisons; thisstage observes378at0.5/1.5/2atm. '
                                            'Four batches percondition; geometric ratios. Existing anchors visible; '
                                            'Per-wave screening only,not campaign-wide error control, '
                                            'independent confirmation or global equivalence.'),
                geometry_policy='Central start; maximum audited width times pressure across all mixtures '
                                'at the same family/recoil/energy, divided by requested pressure. '
                                'Every new raw batch independently requires complete containment; no scaled outputs.',
                gates='27low-energy b0 production canaries,then27high-energy1MeV b0 canaries,then702otherbatches. '
                      'Exact executed inputs,seeds,bindings,composition,density,all50contained endpoints,noescape/edge signal, '
                      'normal completion andvalidated authenticated-noreset displays. New independent seeds disjoint '
                      'from predecessor plans,8316bulk batch seeds and1841legacy surface records. '
                      'Fixed4x50ions/point,5%range/2%ionization SEM goals;flags retained,noadaptive topups. '
                      '2268completecombinedpoints,252at85%noble. Manualcallback beforeG5-85B; noautomaticnextwave.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen G5-85A plan changed')
    if not path.exists():
        write_json(path, plan)
    for case in cases:
        for name, content in zip(('TRIM.IN','TRIM.DAT'),inputs[case['name']]):
            target=run/'inputs'/case['name']/name
            if target.exists() and target.read_bytes()!=content:
                raise ValueError('Frozen worker input changed')
            if not target.exists():
                target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(content)
    budget=run/'budget.json'
    if not budget.exists():write_json(budget,dict(used_seconds=0.,limit_seconds=BUDGET))
    saved=json.loads(budget.read_text())
    if saved['limit_seconds']!=BUDGET or not 0<=saved['used_seconds']<BUDGET:
        raise ValueError('Invalid stage budget')
    return plan, inputs, retained


def audit_case(case, row, contents):
    original_audit(case, row, contents)
    if (not Path(row['attempt']).is_relative_to(RUN/'cases') or
            row['display_startup'].get('reset_on_last_client') is not False or
            row['display_startup'].get('helper') != str(Path(gate.fixed_display.__file__))):
        raise ValueError('Missing validated noreset lifecycle')


def pressure_checks(rows, spec):
    from scipy.stats import t
    if (spec['noble_percent'] != 85 or spec['comparisons'] != 630 or spec['observed_comparisons'] != 378 or spec['familywise_alpha'] != .05 or spec['ionization_margin'] != [.95, 1.05]
            or spec['pressure_scaled_displacement_margin'] != [.9, 1.1]):
        raise ValueError('Prespecified pressure screen changed')
    groups = {}
    seen = set()
    for row in rows:
        if row['noble_percent'] == 85:
            if row['status'] != 'COMPLETE' or row['ions_confirmed'] != 50 or row['model_id'] != MODEL or row['seed'] in seen:
                raise ValueError('Invalid or duplicate pressure-screen batch')
            seen.add(row['seed'])
            groups.setdefault(b.key(row), []).append(row)
    if len(groups) != 252 or any(len(v) != 4 or {r['repeat'] for r in v} != set(range(4)) for v in groups.values()):
        raise ValueError('Pressure screen requires252four-batch points')
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
    if len(checks) != spec['observed_comparisons']:
        raise ValueError('Pressure screen family changed')
    return checks


def publish(state, rows, retained):
    state.update(stage='G5-85A',worker_process_limit=10,software_temperature_cutoff_c=None,
                 hardware_protections_unchanged=True,diagnostic_histories_scheduled=0,
                 previous_stage_cumulative_hours=11795.09465/3600)
    points = b.p.point_summary(retained+rows); new_points = b.p.point_summary(rows)
    state.update(completed_batches=sum(r['status'] == 'COMPLETE' for r in rows), expected_batches=756,
                 new_points_complete=sum(x['complete'] for x in new_points), expected_new_points=189,
                 combined_points_complete=sum(x['complete'] for x in points),
                 completed_precision_flags=sum(x['complete'] and not x['precision_met'] for x in points))
    for name, value in [('status', state), ('results', rows), ('points', points)]:
        write_json(RUN/(name+'.json'), value)
    if points:
        atomic_text(RUN/'points.csv', csv_text(points, sorted({k for x in points for k in x})))
    columns = ['name', 'status', 'family', 'ion', 'energy_kev', 'noble_percent', 'pressure_atm', 'repeat', 'seed',
               'stopped_ion_mean_dx_mm', 'ionization_fraction', 'wall_seconds', 'attempt']
    atomic_text(RUN/'batches.csv', csv_text(retained+rows, columns))
    body = '<html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><h1>G5-85A: direct pressure/energy grid</h1>'
    body += '<p>Ar/Kr/Xe85/CF4-15; C/F/noble recoils,0.5/1/1.5/2atm,1–1000keV.189newpoints,756fixed independent50-ion batches. '
    body += '27low-energy then27high-energy canaries gate702remaining batches.10workers;temperature logging withoutsoftware cutoff,24/32GiB RAM,zero scan swap,24hcap. '
    body += 'Range = endpoint minus start; ionization fraction is not calibrated detector quenching. '
    body += '302preceding precision flags retained; noautomatic topups orG5-85B.</p><p><a href="points.csv">Combined points/SEM CSV</a> · '
    body += '<a href="batches.csv">Batch CSV</a> · <a href="plan.json">Frozen plan</a> · '
    body += '<a href="../noble_cf4_mixture_pressure_75b/review/index.html">G5-75B audit and PDF</a></p>'
    if (RUN/'pressure.pdf').exists():
        body += '<p><a href="pressure.pdf">Pressure plots</a> · <a href="pressure_checks.csv">378of630 pressure intervals</a></p>'
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
                                 and x['noble_percent'] == 85 and x['energy_kev'] == energy), key=lambda x: x['pressure_atm'])
                    for ax, metric in zip(axes, ('projected_range_mm', 'ionization_fraction')):
                        scales = [x['pressure_atm'] if metric == 'projected_range_mm' else 1 for x in rr]
                        ax.errorbar([x['pressure_atm'] for x in rr], [x[metric]*s for x, s in zip(rr, scales)],
                                    yerr=[x[metric+'_sem']*s for x, s in zip(rr, scales)], marker='.', capsize=2, label=f'{energy:g}keV')
                        ax.set_xlabel('Pressure [atm]'); ax.grid(True, alpha=.3); ax.legend(fontsize=7)
                axes[0].set_ylabel('Pressure × displacement [atm mm]'); axes[0].set_yscale('log')
                axes[1].set_ylabel('SRIM ionization fraction')
                fig.suptitle(f'{family}85/CF4-15, {ion} recoil; batch SEM bars, not simultaneous intervals')
                fig.tight_layout(); pdf.savefig(fig); plt.close(fig)


def execute(deadline):
    group=Path(resource_sample()['cgroup'])
    if (group.name!=UNIT or len(os.sched_getaffinity(0))!=WORKERS
            or (group/'memory.max').read_text().strip()!=str(32*GIB)
            or (group/'memory.swap.max').read_text().strip()!='0'):
        raise RuntimeError('Use bounded ten-worker launcher')
    state=dict(status='RUNNING',stage='G5-85A',phase='cold_review',started_at=datetime.now().astimezone().isoformat())
    rows,retained=[],[]; last=-math.inf
    def stop(*_):scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,stop)
    def update(force=False):
        nonlocal last
        if not force and time.monotonic()-last<5:return
        last=time.monotonic(); used=BUDGET-(deadline-last)
        write_json(RUN/'budget.json',dict(used_seconds=used,limit_seconds=BUDGET))
        state.update(updated_at=datetime.now().astimezone().isoformat(),cumulative_hours=used/3600,resources=resource_sample())
        publish(state,rows,retained)
    try:
        update(True);plan,inputs,retained=prepare(RUN)
        if scan.SHUTDOWN_REQUESTED.is_set() or memory_info()['MemAvailable']<16*GIB:
            raise InterruptedError('Stopped or insufficient RAM')
        config=scan.load_config(b.base.d.CONFIG);context=scan.resolve_execution_context(config)
        runtimes,_=scan.prepare_runtimes(scan.resolve_srim_root(None,config),RUN,WORKERS,context)
        def accept(case):
            row=json.loads((RUN/'cases'/case['name']/'result.json').read_text())
            with gate.retry.parser_context():isolated.audit_case(case,row,inputs[case['name']])
            rows.append(row);update(True)
            print(json.dumps(dict(name=case['name'],status='COMPLETE',wall_seconds=row['wall_seconds'])),flush=True)
        for phase in PHASES:
            pending=[case for case in plan['cases'] if case['phase']==phase]
            state['phase']=phase;update(True)
            isolated.run_batches(pending,runtimes,deadline,accept,update)
            if not {case['name'] for case in pending}<={row['name'] for row in rows}:
                raise ValueError('Production phase incomplete; later phases held')
            state[phase+'_passed']=True;update(True)
        state['phase']='final_raw_audit_and_pressure_screen';update(True)
        gate.verify_hashes(plan['input_sha256']);indexed={row['name']:row for row in rows}
        if len(rows)!=756 or len(indexed)!=756:raise ValueError('Incomplete production cohort')
        with gate.retry.parser_context():
            for case in plan['cases']:isolated.audit_case(case,indexed[case['name']],inputs[case['name']])
        points=b.p.point_summary(retained+rows)
        if len(points)!=2268 or not all(x['complete'] for x in points) or len({x['seed'] for x in retained+rows})!=9072:
            raise ValueError('Combined independent point accounting changed')
        checks=pressure_checks(retained+rows,plan['pressure_screen'])
        write_json(RUN/'pressure_checks.json',checks)
        atomic_text(RUN/'pressure_checks.csv',csv_text(checks,sorted({k for x in checks for k in x})))
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill',0)):
            raise RuntimeError('OOM observed')
        plots(points)
        if scan.SHUTDOWN_REQUESTED.is_set() or time.monotonic()>=deadline:raise InterruptedError('Stopped or budget exhausted')
        state.update(status='REVIEW_REQUIRED',phase='G5-85A complete; review before85B at2.5/3atm',source_unchanged=True,
                     combined_independent_batches=9072,combined_histories=453600,
                     pressure_followup_intervals=sum(x['classification']=='followup' for x in checks),
                     longest_batch_seconds=max(x['wall_seconds'] for x in rows))
        update(True);write_json(RUN/'completion.json',state);return 0
    except Exception as exc:
        state.update(status='NEEDS_ATTENTION',error=str(exc));update(True)
        print(str(exc),file=sys.stderr,flush=True);return 1


def prepare_entry():
    plan,inputs,retained=prepare(RUN)
    return plan,inputs,retained,[]


def bind():
    monitor.RUN,monitor.UNIT,monitor.__file__=RUN,UNIT,__file__
    monitor.bind()
    isolated.prepare,isolated.execute,isolated.publish=prepare_entry,execute,publish


if __name__=='__main__':
    bind()
    raise SystemExit(isolated.main())
