#!/usr/bin/env python3
"""M3: fixed direct concentration scan at 1 atm; manual review before P4."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
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
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import review_pressure_batches as previous
from scripts import xvfb_upgrade_gate as environment
from scripts import historical_execution as history
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB
from srim_gas_sweep import load_elements

prior, b, scan = previous.prior.prior, previous.b, previous.scan
RUN_NAME = 'noble_cf4_mixtures_1atm'
RUN = ROOT / 'runs' / RUN_NAME
UNIT = 'srim-mixtures-1atm.service'
MODEL = previous.prior.MODEL
WORKERS = 15
BUDGET = 24 * 3600
JOB_TIMEOUT = 3 * 3600
FRACTIONS = (65, 70, 75, 85, 90)
ENERGIES = (1., 3., 10., 30., 100., 300., 1000.)


def review_environment():
    plan, inputs = environment.prepare(environment.RUN)
    completion = json.loads((environment.RUN/'completion.json').read_text())
    if (completion['status'] != 'REVIEW_REQUIRED' or not completion['source_unchanged']
            or not completion['exact_replay_passed'] or completion['display_checks_passed'] != 60
            or completion['new_independent_production_histories'] != 0):
        raise ValueError('Current environment gate incomplete')
    row = json.loads((environment.RUN/'replay.json').read_text())
    environment.worker.audit_case(plan['replay_case'], row, inputs)
    if not row['replay_passed']:
        raise ValueError('Current environment replay changed')
    stress = json.loads((environment.RUN/'display-stress/result.json').read_text())
    stress_plan = json.loads((environment.RUN/'display-stress/plan.json').read_text())
    if stress['status'] != 'COMPLETE' or len(stress['checks']) != 60 or stress['completed_displays'] != 60:
        raise ValueError('Current display checks incomplete')
    for wave in range(4):
        checks = [c for c in stress['checks'] if c['wave'] == wave]
        if (len(checks) != 15 or len({c['display'] for c in checks}) != 15
                or any(not c['server_reaped'] or not c['unauthorized_client_rejected'] for c in checks)):
            raise ValueError('Current display isolation/cleanup changed')
    pins = {**plan['current_execution_sha256'], **stress_plan['input_sha256'],
            **stress['artifact_sha256'], **row['artifact_sha256']}
    if any(history.actual_sha256(path) != digest for path, digest in pins.items()):
        raise ValueError('Current environment provenance changed')
    return dict(status='AUDITED_CURRENT_ENVIRONMENT_GATE_PASSED', completion=completion, verified_hashes=len(pins))


def check_input_gas(content, condition):
    lines = content.decode('ascii').splitlines()
    layer = prior.layer_fields(content)
    if len(layer) != 7 or layer[1] != condition.condition_id:
        raise ValueError('Input gas layer identity changed')
    if not math.isclose(float(layer[3]), condition.density_g_cm3, rel_tol=1e-12):
        raise ValueError('Input gas density mismatch')
    symbols = ('C', 'F', condition.family)
    for value, symbol in zip(layer[4:], symbols):
        if not math.isclose(float(value), condition.atomic_fractions[symbol], rel_tol=1e-12):
            raise ValueError('Input molecular-to-atomic stoichiometry mismatch')
    atoms = [re.fullmatch(r'Atom (\d+) = (\w+) =\s*(\d+)\s+([\d.Ee+\-]+)', line) for line in lines if line.startswith('Atom ')]
    if len(atoms) != 3 or any(m is None for m in atoms) or tuple(m[2] for m in atoms) != symbols:
        raise ValueError('Input target species/order mismatch')
    for atom in atoms:
        if int(atom[3]) != {'C': 6, 'F': 9, 'Ar': 18, 'Kr': 36, 'Xe': 54}[atom[2]]:
            raise ValueError('Input target atomic number mismatch')
        if not math.isclose(float(atom[4]), condition.element_masses_u[atom[2]], rel_tol=1e-12):
            raise ValueError('Input target atomic mass mismatch')
    phase = lines[next(i for i, line in enumerate(lines) if 'Target layer phases' in line)+1].strip()
    correction = lines[lines.index('Target Compound Corrections (Bragg)')+1].strip()
    if phase != '1' or correction != '1':
        raise ValueError('Input gas phase/compound correction changed')


def validate_input(case, contents, condition):
    content, events = contents; check_input_gas(content, condition)
    if hashlib.sha256(content).hexdigest() != case['input_sha256'] or hashlib.sha256(events).hexdigest() != case['trim_dat_sha256']:
        raise ValueError('Input fingerprint changed')
    if b'\n' in content.replace(b'\r\n', b'') or events[-1:] != b'\x1a':
        raise ValueError('Input CRLF/DOS EOF changed')
    lines = content.decode('ascii').splitlines(); layer = prior.layer_fields(content)
    fields = lines[next(i for i, x in enumerate(lines) if x.startswith('Cascades('))+1].split()
    if fields != ['5', str(case['seed']), '0']:
        raise ValueError('Mode/seed changed')
    target = lines[lines.index('Target material : Number of Elements, Number of Layers')+1]
    disk = lines[next(i for i, x in enumerate(lines) if x.startswith('Diskfiles'))+1].split()
    plot = lines[next(i for i, x in enumerate(lines) if x.startswith('PlotType'))+1].split()
    if target != f'"{case["name"]}" 3 1' or disk[:3] != ['1', '1', '1'] or [float(v) for v in plot] != [5., 0., case['width_angstrom']]:
        raise ValueError('Input target/output configuration changed')
    ion = lines[next(i for i, x in enumerate(lines) if x.startswith('Ion:'))+1].split()
    if int(ion[0]) != case['ion_z'] or float(ion[2]) != case['energy_kev'] or int(ion[4]) != 50 or float(ion[3]) != 0:
        raise ValueError('Input primary parameters changed')
    if not math.isclose(float(ion[1]), condition.element_masses_u[case['ion']], rel_tol=1e-12):
        raise ValueError('Input primary mass changed')
    if float(layer[2]) != case['width_angstrom'] or case['origin_angstrom'] != case['width_angstrom']/2:
        raise ValueError('Input bulk geometry changed')
    for label, value in [('displacement', 21.), ('lattice binding', 1e-9), ('surface binding', 1e-9)]:
        index = next(i for i, x in enumerate(lines) if x.startswith('Individual target atom '+label+' energies'))
        if [float(v) for v in lines[index+1].split()] != [value]*3:
            raise ValueError('Input binding model changed')
    event_lines = events[:-1].decode('ascii').splitlines()
    if len(event_lines) != 60:
        raise ValueError('Expected exactly fifty input events')
    for index, line in enumerate(event_lines[10:], 1):
        fields = line.split()
        expected = [float(case['ion_z']), case['energy_kev']*1000, case['origin_angstrom'], 0., 0., 1., 0., 0.]
        if fields[0] != f'E{index:04d}' or [float(v) for v in fields[1:]] != expected:
            raise ValueError('Input event identity/energy/coordinates changed')


def make_input(original, condition, name, seed, z, energy, width):
    if condition.noble_percent not in FRACTIONS or condition.pressure_atm != 1. or width <= 0 or not math.isfinite(width):
        raise ValueError('Unexpected mixture or geometry')
    lines = original.decode('ascii').splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith('1 "'))
    fractions = ' '.join(f'{condition.atomic_fractions[s]:.17g}' for s in ('C', 'F', condition.family))
    lines[index] = f'1 "{condition.condition_id}" {width/2:.17g} {condition.density_g_cm3:.17g} {fractions}'
    return b.make_input(('\r\n'.join(lines)+'\r\n').encode('ascii'), name, seed, z, energy, 2)


def known_seeds(rows):
    seeds = {r['seed'] for r in rows}; paths = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('seed', 'random_seed') and isinstance(child, int) and child > 0:
                    seeds.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for path in sorted((ROOT/'runs').glob('*/plan.json')):
        if path.parent.name == RUN_NAME:
            continue
        visit(json.loads(path.read_text())); paths.append(path)
    return seeds, paths


def forecast(cases):
    phases = {}
    for phase in ('composition_gate', 'mixture_production'):
        loads = [0.]*WORKERS
        for cost in sorted((c['estimated_seconds'] for c in cases if c['phase'] == phase), reverse=True):
            heapq.heapreplace(loads, loads[0]+cost)
        phases[phase] = max(loads)/3600
    model = sum(phases.values())
    return dict(model_hours=model, planning_hours=1.5*model+.5, phase_model_hours=phases,
                longest_estimated_batch_hours=max(c['estimated_seconds'] for c in cases)/3600,
                method='Maximum audited four-batch mean runtime of adjacent concentration anchors '
                       '(60/80% or 80/95%), same family/recoil/energy at 1atm. '
                       'Fifteen-worker longest-first scheduling, low-energy gate then remaining batches, '
                       '50% margin plus30min. Conservative runtime planning, not interpolated physics.')


def prepare(run):
    env_review = review_environment()
    review, retained, _, timing, _ = previous.review()
    if review['pressure_classifications'].get('followup', 0):
        raise ValueError('Material pressure feature requires review before mixture release')
    config = scan.load_config(b.base.d.CONFIG); root = scan.resolve_srim_root(None, config)
    elements, _ = load_elements(root/'Data/ATOMDATA')
    config = replace(config, noble_percent=(60, 65, 70, 75, 80, 85, 90, 95), pressures_atm=(1.,))
    conditions = {(c.family, c.noble_percent): c for c in scan.generate_conditions(config, elements)}
    sources = {}
    for row in sorted(retained, key=lambda r: r['repeat']):
        if row['pressure_atm'] == 1.:
            sources.setdefault(b.key(row), row)
    if len(sources) != 189:
        raise ValueError('Expected 189 audited 1-atm anchor points')
    for key, row in sources.items():
        check_input_gas((Path(row['attempt'])/'TRIM.IN').read_bytes(), conditions[key[0], key[3]])
    costs = {b.key(t): t['mean_seconds'] for t in timing}
    forbidden, seed_paths = known_seeds(retained); used = set(forbidden)
    cases, inputs = [], {}
    paths = {Path(__file__), ROOT/'scripts/mixtures_1atm.sh', Path(previous.__file__),
             Path(environment.__file__), Path(history.__file__), Path(b.base.d.CONFIG),
             previous.RUN/'plan.json', previous.RUN/'completion.json', previous.RUN/'results.json',
             previous.RUN/'points.json', previous.RUN/'pressure_checks.json',
             environment.RUN/'plan.json', environment.RUN/'completion.json', environment.RUN/'replay.json'}
    paths.update(seed_paths)
    paths.update(Path(p) for p in json.loads((environment.RUN/'plan.json').read_text())['current_execution_sha256'])
    context = scan.resolve_execution_context(config)
    if context.pysrim_root:
        paths.update((context.pysrim_root/'srim').rglob('*.py'))
    for family in ('Ar', 'Kr', 'Xe'):
        for ion in ('C', 'F', family):
            for energy in ENERGIES:
                for noble in FRACTIONS:
                    anchors = (60, 80) if noble < 80 else (80, 95)
                    rows = [sources[family, ion, energy, n, 1.] for n in anchors]
                    width = max(row['width_angstrom'] for row in rows)
                    cost = max(costs[family, ion, energy, n, 1.] for n in anchors)
                    template = rows[0]; source = Path(template['attempt'])/'TRIM.IN'
                    paths.add(source); paths.add(Path(template['source']))
                    condition = conditions[family, noble]
                    point = b.name_for((family, ion, energy, noble, 1.))
                    for repeat in range(4):
                        name = point+f'_b{repeat}'; seed = scan.seed_for(MODEL+'-mixtures-1atm-'+point, repeat)
                        if seed in used:
                            raise ValueError('New mixture seed collision; never silently replace a seed')
                        used.add(seed)
                        content, events, origin, target = make_input(source.read_bytes(), condition, name, seed, template['ion_z'], energy, width)
                        case = dict(name=name, point=point, repeat=repeat, phase='composition_gate' if energy == 1. and repeat == 0 else 'mixture_production',
                                    diagnostic=False, model_id=MODEL, family=family, ion=ion, ion_z=template['ion_z'], energy_kev=energy,
                                    noble_percent=noble, pressure_atm=1., geometry='interior', ions=50, seed=seed, source=template['source'],
                                    template_input=str(source), geometry_anchor_points=[r['point'] for r in rows],
                                    condition_id=condition.condition_id, density_g_cm3=condition.density_g_cm3,
                                    atomic_fractions=condition.atomic_fractions, mass_fractions=condition.mass_fractions,
                                    origin_angstrom=origin, width_angstrom=target, expected=template['expected'],
                                    input_sha256=hashlib.sha256(content).hexdigest(), trim_dat_sha256=hashlib.sha256(events).hexdigest(),
                                    estimated_seconds=cost)
                        validate_input(case, (content, events), condition)
                        cases.append(case); inputs[name] = (content, events)
    estimate = forecast(cases)
    if (len(cases) != 1260 or len({c['point'] for c in cases}) != 315 or len(used-forbidden) != 1260
            or sum(c['phase'] == 'composition_gate' for c in cases) != 45 or estimate['planning_hours'] > 21
            or estimate['longest_estimated_batch_hours'] > 2.5):
        raise ValueError(f'M3 count, independence or drain-budget gate failed: {estimate}')
    plan = dict(stage='M3', model_id=MODEL, cases=cases, total_cases=1260, new_points=315, new_histories=63000,
                retained_points=261, retained_independent_batches=1044, combined_points_after_stage=576,
                combined_independent_batches_after_stage=2304, combined_histories_after_stage=115200,
                complete_1atm_points_after_stage=504, independent_batches_per_point=4, workers=WORKERS,
                budget_hours=24, job_timeout_seconds=JOB_TIMEOUT, forecast=estimate,
                previous_review=review, environment_review=env_review, anchor_input_gas_bridges=189,
                forbidden_previous_seed_count=len(forbidden), forbidden_previous_seeds_sha256=hashlib.sha256(json.dumps(sorted(forbidden)).encode()).hexdigest(),
                gas_conditions=[asdict(c) for (f, n), c in sorted(conditions.items()) if n in FRACTIONS],
                input_sha256={str(p): history.actual_sha256(p) for p in sorted(paths)},
                design='Ar/Kr/Xe, C/F/noble recoils,65/70/75/85/90% noble at1atm,293.15K, '
                       '1/3/10/30/100/300/1000keV. Four new independent50-ion batches perpoint; no surface/diagnostic reuse. '
                       'Canonical ideal-gas density and molecular-to-atomic fractions first reproduce all189 old1atm anchors. '
                       'Keep mode5, displacement21eV,lattice/surface1e-9eV,gas phase and unity compound correction. '
                       'Central start in max full-width of adjacent audited concentration anchors; every batch must remain contained. '
                       'No simulated observable is interpolated.',
                gates='First45 1keV b0 composition/containment canaries are production samples and must all pass before '
                      'remaining1215 batches. Exact input/event/seed hashes, actual species/energy/bindings, printed density '
                      'and atomic/mass fractions within one last-printed-digit unit; all50 endpoints, no escape/edge signal, '
                      'normal completion and private display/helper evidence. Fixed four-batch SEM targets: '
                      'range5%,ionization2%; flags retained for Q6, no automatic resampling.',
                next='Stop at REVIEW_REQUIRED,NEEDS_ATTENTION orSTOPPED. Manual callback before P4; '
                     'review mixture curves/timing/precision and independently confirm any selected feature before physical claims. '
                     'Range is endpoint minus start; ionization fraction is not detector-calibrated quenching.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen M3 plan changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs, retained


def check_composition(case, outputs):
    text = scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
    matches = re.findall(r'Layer #\s*1-\s*(\w+)\s*=\s*([\d.Ee+\-]+)\s+Atomic Percent\s*=\s*([\d.Ee+\-]+)\s+Mass Percent', text)
    if len(matches) != 3 or {m[0] for m in matches} != {'C', 'F', case['family']}:
        raise ValueError('Actual target composition species mismatch')
    reported = {}
    for symbol, atom, mass in matches:
        entry = {}
        for field, raw in [('atomic', atom), ('mass', mass)]:
            value = float(raw); quantum = float(Decimal(10)**Decimal(raw).as_tuple().exponent)
            expected = 100*case[field+'_fractions'][symbol]
            if not math.isfinite(value) or value <= 0 or quantum > .1 or abs(value-expected) > quantum+1e-10:
                raise ValueError('Actual target '+field+' fraction mismatch')
            entry[field+'_percent'] = value; entry[field+'_resolution_percent'] = quantum
        reported[symbol] = entry
    if not re.search(r'Layer #\s*1\s*-\s*'+re.escape(case['condition_id'])+r'\s*$', text, re.MULTILINE):
        raise ValueError('Actual gas layer identity mismatch')
    return dict(reported_composition=reported, composition_parameters_match=True)


def audit_case(case, row, contents):
    prior.audit_case(case, row, contents)
    directory = Path(row['attempt'])
    if str(directory/'.completion-probes.json') not in row['artifact_sha256']:
        raise ValueError('Missing raw completion-monitor fingerprint')
    fresh = check_composition(case, {p.name.upper(): p for p in directory.iterdir()})
    if any(row[k] != v for k, v in fresh.items()):
        raise ValueError('Composition analysis changed')


def run_case(case, contents, runtime, context, deadline):
    (runtime/'.completion-probes.json').unlink(missing_ok=True)
    row = b.run_case(case, contents, runtime, context, deadline)
    metadata = runtime/'.completion-probes.json'
    if metadata.exists():
        target = Path(row['attempt'])/metadata.name; shutil.copy2(metadata, target)
        row['artifact_sha256'][str(target)] = history.actual_sha256(target)
        row['completion_monitor'] = json.loads(target.read_text())
    if row['status'] == 'COMPLETE':
        try:
            outputs = scan.collect_runtime_outputs(runtime)
            row.update(prior.check_density(case, outputs)); row.update(check_composition(case, outputs))
            audit_case(case, row, contents)
        except Exception as exc:
            row.update(status='FAILED', error=str(exc))
    write_json(RUN/'cases'/case['name']/'result.json', row)
    return row


def publish(state, rows, retained):
    points = b.p.point_summary(rows); combined = b.p.point_summary(retained+rows)
    state.update(completed_new_batches=sum(r['status'] == 'COMPLETE' for r in rows), expected_new_batches=1260,
                 new_points_complete=sum(p['complete'] for p in points), expected_new_points=315,
                 combined_points_complete=sum(p['complete'] for p in combined),
                 new_points_meeting_precision=sum(p['precision_met'] for p in points),
                 combined_completed_precision_flags=sum(p['complete'] and not p['precision_met'] for p in combined))
    for name, data in [('status', state), ('results', rows), ('points', combined)]:
        write_json(RUN/(name+'.json'), data)
    if combined:
        atomic_text(RUN/'points.csv', csv_text(combined, list(combined[0])))
    columns = ['name', 'status', 'family', 'ion', 'energy_kev', 'noble_percent', 'pressure_atm', 'repeat', 'seed',
               'ions_confirmed', 'stopped_ion_mean_dx_mm', 'ionization_fraction', 'wall_seconds', 'attempt']
    atomic_text(RUN/'batches.csv', csv_text(retained+rows, columns))
    body = '<html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><title>M3 direct mixtures</title>'
    body += '<h1>M3: Ar/Kr/Xe concentration scan at 1 atm</h1><p>315 new points,1260 independent50-ion batches. '
    body += '65/70/75/85/90% noble, C/F/noble recoils,1/3/10/30/100/300/1000keV; 200 ions/point. '
    body += '45 low-energy b0 batches gate remaining production. Direct results only; partial points have no certified precision. '
    body += '15-core maximum,24/32GiB RAM,zero scan swap,24-hour cumulative cap and3-hour job cap. '
    body += 'No automatic P4 or topups. Range = endpoint minus start; ionization fraction is not detector-calibrated quenching.</p>'
    body += '<p><a href="points.csv">Combined points and SEM CSV</a> · <a href="batches.csv">Batches CSV</a> · '
    body += '<a href="plan.json">Frozen plan</a> · <a href="logs/campaign.log">Log</a></p>'+table([state], list(state))
    body += '<p>46 preceding precision flags remain; new flags are assessed only after four batches, without adaptive sampling.</p>'
    if (RUN/'mixtures.pdf').exists():
        body += '<p><a href="mixtures.pdf">Mixture PDF</a></p>'
    atomic_text(RUN/'index.html', body)


def plots(rows):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    points = [p for p in b.p.point_summary(rows) if p['complete'] and p['pressure_atm'] == 1.]
    with plt.rc_context({'font.size': 9}):
        fig, axes = plt.subplots(3, 2, figsize=(12, 12))
        for i, family in enumerate(('Ar', 'Kr', 'Xe')):
            for noble in (60, 65, 70, 75, 80, 85, 90, 95):
                selected = sorted((p for p in points if p['family'] == family and p['ion'] == family and p['noble_percent'] == noble), key=lambda p: p['energy_kev'])
                for j, metric in enumerate(('projected_range_mm', 'ionization_fraction')):
                    axes[i, j].errorbar([p['energy_kev'] for p in selected], [p[metric] for p in selected],
                                       yerr=[p[metric+'_sem'] for p in selected], marker='.', capsize=2, label=f'{noble}%')
                    axes[i, j].set_xscale('log'); axes[i, j].set_xlabel('Recoil energy [keV]'); axes[i, j].grid(True, alpha=.3)
                    axes[i, j].set_title(f'{family} recoils in {family}/CF4, 1 atm')
                    axes[i, j].set_ylabel('Projected displacement [mm]' if j == 0 else 'SRIM ionization fraction')
                    if j == 0:
                        axes[i, j].set_yscale('log')
                    axes[i, j].legend(ncol=2, fontsize=7)
        fig.suptitle('Direct noble-recoil mixture curves; 200 ions/point, error bars = estimated batch SEM\n'
                     'C/F recoil results are also in the CSV. No detector-quenching or feature-confirmation claim.')
        fig.tight_layout(); fig.savefig(RUN/'mixtures.png', dpi=120)
        with PdfPages(RUN/'mixtures.pdf') as pdf:
            pdf.savefig(fig); plt.close(fig)
            for family in ('Ar', 'Kr', 'Xe'):
                for ion in ('C', 'F', family):
                    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
                    for energy in ENERGIES:
                        selected = sorted((p for p in points if p['family'] == family and p['ion'] == ion and p['energy_kev'] == energy), key=lambda p: p['noble_percent'])
                        for j, metric in enumerate(('projected_range_mm', 'ionization_fraction')):
                            axes[j].errorbar([p['noble_percent'] for p in selected], [p[metric] for p in selected],
                                             yerr=[p[metric+'_sem'] for p in selected], marker='o', capsize=2, label=f'{energy:g} keV')
                            axes[j].set_xlabel(f'{family} molecular fraction [%]'); axes[j].set_xticks(range(60, 100, 5))
                            axes[j].set_ylabel('Projected displacement [mm]' if j == 0 else 'SRIM ionization fraction')
                            axes[j].grid(True, alpha=.3); axes[j].legend(fontsize=8)
                            if j == 0:
                                axes[j].set_yscale('log')
                    fig.suptitle(f'{ion} recoils in {family}/CF4 at 1atm: direct mixture scan\n'
                                 'Fixed four independent batches; estimated SEM bars, not model accuracy or detector quenching')
                    fig.tight_layout(); pdf.savefig(fig); plt.close(fig)


def execute():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > WORKERS
            or (group/'memory.max').read_text().strip() != str(32*GIB) or (group/'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use the resource-limited M3 launcher')
    used = json.loads((RUN/'budget.json').read_text())['used_seconds'] if (RUN/'budget.json').exists() else 0.
    if used >= BUDGET or memory_info()['MemAvailable'] < 16*GIB:
        raise RuntimeError('Insufficient cumulative time/RAM allowance')
    if (RUN/'completion.json').exists():
        raise RuntimeError('Already complete; review instead of relaunching')
    started = time.monotonic(); deadline = started+BUDGET-used; rows, retained = [], []
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), phase='preparing_audited_mixtures', model_id=MODEL)
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
            cached = b.base.cached(case)
            if cached:
                audit_case(case, cached, inputs[case['name']]); rows.append(cached); cached_names.add(case['name'])
        for phase in ('composition_gate', 'mixture_production'):
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
                raise RuntimeError('M3 integrity failure; hold dependent production')
            if scan.SHUTDOWN_REQUESTED.is_set():
                raise InterruptedError('Stopped or insufficient cumulative budget')
            expected = {c['name'] for c in plan['cases'] if c['phase'] == phase}
            if not expected <= {r['name'] for r in rows if r['status'] == 'COMPLETE'}:
                raise ValueError('Incomplete M3 phase gate')
            if phase == 'composition_gate':
                state['composition_gate_passed'] = True; update()
        prepare(RUN)
        indexed = {r['name']: r for r in rows}
        if len(rows) != 1260 or len(indexed) != 1260:
            raise ValueError('Incomplete M3 cohort')
        for case in plan['cases']:
            audit_case(case, indexed[case['name']], inputs[case['name']])
        points = b.p.point_summary(retained+rows)
        if len(points) != 576 or not all(p['complete'] for p in points) or sum(p['pressure_atm'] == 1. for p in points) != 504:
            raise ValueError('Incomplete combined concentration grid')
        if int(dict(line.split() for line in (group/'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('OOM observed')
        plots(retained+rows)
        state.update(status='REVIEW_REQUIRED', phase='M3 complete; review mixture curves, timings and precision before P4', source_unchanged=True,
                     combined_independent_batches=2304, combined_histories=115200,
                     longest_batch_seconds=max(r['wall_seconds'] for r in rows))
        update(); write_json(RUN/'completion.json', state); return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(RUN); print(json.dumps({k: plan[k] for k in ('stage', 'total_cases', 'new_points', 'new_histories', 'forecast')}, indent=2)); return 0
        b.RUN, b.base.RUN, scan.launch_command = RUN, RUN, prior.r.launch
        return execute()


if __name__ == '__main__':
    raise SystemExit(main())
