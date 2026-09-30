#!/usr/bin/env python3
"""Fixed TRIM.DAT interior-start feasibility test; never production statistics."""
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
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import pressure_diagnostic as d
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample, memory_info, GIB
from trim_qf_sweep import numeric_columns_from_line
scan = d.scan
RUN = ROOT / 'runs/kr_bulk_geometry_probe'
UNIT = 'srim-bulk-geometry.service'
BUDGET = 3 * 3600
MANUAL = ROOT.parent / 'srim-2013/HELP - TRIM Input.pdf'
OUTPUT_MANUAL = ROOT.parent / 'srim-2013/HELP - TRIM Output.pdf'


def trim_dat(depth, ions=50):
    if not math.isfinite(depth) or depth <= 0 or ions != 50:
        raise ValueError('Expected positive starting depth and exactly 50 events')
    header = ['TRIM.DAT interior-start feasibility test', 'Ten comment lines; ASCII CRLF and DOS EOF',
              'One event per ion; energy in eV; positions in Angstrom',
              'Event ID has exactly five characters', 'F ions, Z=9, E=100000 eV',
              'Fixed direction +X; Y=Z=0', 'Diagnostics only; not independent production',
              'Kr80 CF4-20 F100keV geometry probe',
              'Event Z Energy X Y Z CosX CosY CosZ', 'BEGIN EVENTS AFTER THIS TENTH HEADER LINE']
    records = [f'E{i:04d} 9 100000 {depth:.17g} 0 0 1 0 0' for i in range(1, ions + 1)]
    return ('\r\n'.join(header + records) + '\r\n').encode('ascii') + b'\x1a'


def make_input(original, name, pressure, geometry):
    if geometry not in ('mode2_surface', 'near_surface', 'interior', 'interior_deep'):
        raise ValueError('Unknown geometry')
    scale = {'mode2_surface': 1, 'near_surface': 1, 'interior': 2, 'interior_deep': 4}[geometry]
    content = d.variant(original, name, pressure, scale, scale)
    lines = content.decode('ascii').splitlines()
    layer = shlex.split(next(x for x in lines if x.startswith('1 "')))
    width = float(layer[2])
    mode = 2 if geometry == 'mode2_surface' else 5
    i = next(i for i, x in enumerate(lines) if x.startswith('Cascades(')) + 1
    fields = lines[i].split(); fields[0] = str(mode); lines[i] = ' '.join(fields)
    i = next(i for i, x in enumerate(lines) if x.startswith('Diskfiles')) + 1
    fields = lines[i].split(); fields[0] = '1'; lines[i] = ' '.join(fields)
    depth = 0. if mode == 2 else 1. if geometry == 'near_surface' else width / 2
    return ('\r\n'.join(lines) + '\r\n').encode('ascii'), (trim_dat(depth) if mode == 5 else None), depth, width


def endpoints(text, ions, escaped, width, origin):
    records = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 4 and re.fullmatch(r'\d+', fields[0]):
            records.append((int(fields[0]), *(float(x) for x in fields[1:])))
    if len(records) != ions - escaped or len({r[0] for r in records}) != len(records) or not records:
        raise ValueError('RANGE_3D endpoint count/uniqueness mismatch')
    if any(not 1 <= r[0] <= ions or not all(math.isfinite(v) for v in r[1:]) or not 0 <= r[1] <= width for r in records):
        raise ValueError('Invalid endpoint identity/position')
    return dict(endpoint_count=len(records), endpoint_mean_x_mm=statistics.mean(r[1] for r in records) * 1e-7,
                stopped_ion_mean_dx_mm=statistics.mean(r[1] - origin for r in records) * 1e-7,
                stopped_ion_min_x_angstrom=min(r[1] for r in records),
                stopped_ion_max_x_angstrom=max(r[1] for r in records),
                range_interpretation='Mean X endpoint minus specified starting X, over stopped primary ions only; escaped ions excluded explicitly.')


def review_previous():
    plan, _ = d.prepare(d.RUN)
    results = [d.cached_result(c) for c in plan['cases']]
    if len(results) != 36 or any(r is None or r['status'] != 'COMPLETE' for r in results):
        raise ValueError('D0 incomplete or corrupt')
    if not d.replay_gate([r for r in results if r['phase'] == 'replay']):
        raise ValueError('D0 replay gate not passed')
    by_name = {r['name']: r for r in results}
    crosses = []
    for repeat in (100, 101, 102):
        a, b = (by_name[f'paired_r{repeat}_p{p}'] for p in (2.2, 2.3))
        crosses.append(dict(repeat=repeat, qf_relative_change=b['ionization_fraction'] / a['ionization_fraction'] - 1,
                            pressure_range_relative_change=b['pressure_times_range_mm_atm'] / a['pressure_times_range_mm_atm'] - 1))
    comparisons = d.comparisons(results)
    summary = dict(status='D0_AUDITED_GEOMETRY_FEASIBILITY_REQUIRED', cases=36, replays_passed=3,
                   completion=json.loads((d.RUN / 'completion.json').read_text()),
                   max_depth_binning_qf_relative_change=max(abs(r['qf_relative_difference']) for r in comparisons if r['phase'] == 'geometry'),
                   max_depth_binning_range_relative_change=max(abs(r['pressure_range_relative_difference']) for r in comparisons if r['phase'] == 'geometry'),
                   max_paired_pressure_qf_relative_change=max(abs(r['qf_relative_difference']) for r in comparisons if r['phase'] == 'paired'),
                   max_paired_pressure_range_relative_change=max(abs(r['pressure_range_relative_difference']) for r in comparisons if r['phase'] == 'paired'),
                   density_header_crossings=crosses,
                   interpretation='No tenfold transport jump at the printed-density exponent transition; supports a display issue, not universal proof. '
                   'Three selected reused seeds cannot establish statistical pressure equivalence. '
                   'Bulk/interior geometry remains unvalidated; insert C2a before P2a.',
                   documentation=dict(local_input=str(MANUAL), local_output=str(OUTPUT_MANUAL),
                                      input_pages='8-14 and 8-15', output_pages='9-13 and 9-14',
                                      online='https://www.srim.org/SRIM/SRIM%2008.pdf'))
    return summary, results


def prepare(run):
    review, old = review_previous()
    rows = d.prior.fresh_rows(d.prior.RUN)
    records = {r['repeat']: r for r in rows if (r['family'], r['ion'], r['energy_kev'], r['noble_percent'], r['pressure_atm'])
               == ('Kr', 'F', 100, 80, 1)}
    cases, inputs = [], {}
    paths = {Path(__file__), ROOT / 'scripts/bulk_geometry_probe.sh', MANUAL, OUTPUT_MANUAL,
             ROOT.parent / 'pysrim/srim/srim.py', ROOT.parent / 'srim-2013/TRIM.dat',
             d.RUN / 'plan.json', d.RUN / 'completion.json', d.RUN / 'results.json'}
    for r in old:
        paths.update(Path(p) for p in r['artifact_sha256'])
    specs = [(100, 1., 'mode2_surface'), (100, 1., 'near_surface')]
    specs += [(repeat, p, geometry) for repeat in (100, 101, 102) for p in (.5, 1., 3.)
              for geometry in ('near_surface', 'interior', 'interior_deep')
              if (repeat, p, geometry) != (100, 1., 'near_surface')]
    for repeat, p, geometry in specs:
        row = records[repeat]
        source = Path(row['raw_archive']); paths.add(source)
        name = f'{geometry}_r{repeat}_p{p:g}'
        content, events, depth, width = make_input(d.archive_data(source, 'TRIM.IN'), name, p, geometry)
        inputs[name] = (content, events)
        cases.append(dict(name=name, phase='canary' if len(cases) < 2 else 'variants', geometry=geometry,
                          repeat=repeat, pressure_atm=p, source=str(source), seed=row['random_seed'],
                          ions=50, origin_angstrom=depth, width_angstrom=width,
                          input_sha256=hashlib.sha256(content).hexdigest(),
                          trim_dat_sha256=hashlib.sha256(events).hexdigest() if events else None,
                          expected=row['target_energy_check']['expected'],
                          reference_qf=row['qf_trim_ionization'], reference_range_mm=row['projected_range_mm']))
    if len(cases) != 28:
        raise ValueError('Expected 28 fixed geometry cases')
    plan = dict(model_id='candidate-trim5-interior-eps1e9-v1', production_model_unchanged=d.prior.MODEL,
                cases=cases, total_cases=28, diagnostic_histories=1400, independent_production_histories_added=0,
                input_sha256={str(p): scan.sha256_file(p) for p in sorted(paths)},
                previous_review=review, planning_minutes=60, budget_hours=3,
                design='One mode-2 surface endpoint control; mode-5 near-surface (1 Angstrom), interior W in 2W target, '
                       'and interior 2W in 4W target; three archived seeds at .5/1/3 atm, F100keV Kr80. '
                       'TRIM.DAT has 50 explicit events, 10 headers, CRLF and DOS EOF; RANGE_3D requested.',
                gates='Two canaries before variants. Mode-2 observable replay must agree; all executed inputs/bindings/ion counts/endpoints audited. '
                      'Interior cases require zero primary escape and profile containment. '
                      'No equivalence, bulk-model adoption or production release based on this small diagnostic.',
                range_note='Save raw summary and endpoint-minus-origin separately; do not assume RANGE.TXT already subtracts starting depth.')
    path = run / 'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen bulk probe changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs


def cached(case):
    path = RUN / 'cases' / case['name'] / 'result.json'
    if not path.exists(): return None
    r = json.loads(path.read_text())
    if r['status'] == 'FAILED': raise RuntimeError('Inspect failed case before resume: ' + case['name'])
    if r['status'] != 'COMPLETE': return None
    if any(r[k] != case[k] for k in ('input_sha256', 'trim_dat_sha256')):
        raise ValueError('Cached input identity changed')
    for path, digest in r['artifact_sha256'].items():
        if scan.sha256_file(Path(path)) != digest: raise ValueError('Cached raw artifact changed')
    return r


def analyse(case, outputs):
    for name in (*scan.STANDARD_TRIM_OUTPUTS, 'RANGE_3D.TXT', 'BACKSCAT.TXT', 'TRANSMIT.TXT'):
        scan.find_output(outputs, name)
    text = scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
    if not scan.check_target_energies(text, case['expected'])['passed']:
        raise ValueError('Target parameter mismatch')
    count = re.search(r'Total Ions calculated\s*=\s*([\d.]+)', text)
    if count is None or float(count[1]) != 50: raise ValueError('Incomplete event count')
    q = scan.parse_ioniz(scan.find_output(outputs, 'IONIZ.TXT'), 100.)
    ranges = scan.parse_range_summary(scan.find_output(outputs, 'RANGE.TXT'))
    back = scan.count_escape_records(scan.find_output(outputs, 'BACKSCAT.TXT'), 'B')
    transmitted = scan.count_escape_records(scan.find_output(outputs, 'TRANSMIT.TXT'), 'T')
    if not 0 < q.qf_ioniz <= 1 or transmitted or q.estimated_max_depth_angstrom >= .98 * case['width_angstrom']:
        raise ValueError('Ionization bounds, transmission or upper profile clipping')
    if case['geometry'].startswith('interior') and back:
        raise ValueError('Interior primary escaped; geometry not contained')
    if case['geometry'].startswith('interior'):
        profile = [v for line in scan.find_output(outputs, 'IONIZ.TXT').read_text(encoding='latin1').splitlines()
                   if (v := numeric_columns_from_line(line)) is not None]
        totals = [v[1] + v[2] for v in profile]
        if len(totals) != 100 or max(totals[0], totals[-1]) > max(totals) * 1e-9:
            raise ValueError('Interior ionization reaches profile boundary')
    result = endpoints(scan.find_output(outputs, 'RANGE_3D.TXT').read_text(encoding='latin1'),
                       50, back + transmitted, case['width_angstrom'], case['origin_angstrom'])
    if case['geometry'] == 'mode2_surface':
        if not math.isclose(q.qf_ioniz, case['reference_qf'], rel_tol=1e-12) or ranges.projected_range_mm != case['reference_range_mm']:
            raise ValueError('Mode-2 control does not reproduce source observables')
    result.update(ionization_fraction=q.qf_ioniz, reported_range_summary_mm=ranges.projected_range_mm,
                  backscattered_ions=back, transmitted_ions=transmitted,
                  primary_ionization_kev=q.primary_ionization_energy_kev,
                  secondary_ionization_kev=q.recoil_ionization_energy_kev,
                  parameters_match=True, ions_confirmed=50)
    return result


def run_case(case, contents, runtime, context, deadline):
    started = time.monotonic()
    directory = RUN / 'cases' / case['name']; directory.mkdir(parents=True, exist_ok=True)
    attempt = directory / f'attempt-{time.time_ns()}'; attempt.mkdir()
    result = dict(case, status='RUNNING', attempt=str(attempt))
    env = scan.pysrim_environment(context)
    try:
        scan.clear_runtime_outputs(runtime)
        content, events = contents
        (runtime / 'TRIM.IN').write_bytes(content); (attempt / 'TRIM.IN').write_bytes(content)
        (runtime / 'TRIMAUTO').write_bytes(d.archive_data(Path(case['source']), 'TRIMAUTO'))
        (runtime / 'Data/TRIM.cfg').write_bytes(d.archive_data(Path(case['source']), 'Data/TRIM.cfg'))
        if events is not None:
            # Linux/Wine case aliases are unsafe: use the existing runtime name.
            candidates = [p for p in runtime.iterdir() if p.name.lower() == 'trim.dat']
            if len(candidates) != 1: raise ValueError('Expected exactly one runtime TRIM.dat')
            candidates[0].write_bytes(events); (attempt / 'TRIM.DAT').write_bytes(events)
        timeout = min(1800, int(deadline - time.monotonic() - 45))
        if timeout <= 0: raise RuntimeError('Cumulative deadline reached')
        scan.launch_command(['xvfb-run', '-a', '-e', str(runtime / '.xvfb.log'), '-s', '-screen 0 1024x768x24',
                             sys.executable, str(Path(__file__).resolve()), 'worker', '--runtime', str(runtime), '--name', case['name']],
                            runtime, timeout, environment=env, process_group=True, display_name=case['name'])
        if (runtime / 'TRIM.IN').read_bytes() != content: raise ValueError('TRIM.IN changed')
        if events is not None and candidates[0].read_bytes() != events: raise ValueError('TRIM.DAT changed')
        result.update(analyse(case, scan.collect_runtime_outputs(runtime)), status='COMPLETE')
    except Exception as exc:
        result.update(status='STOPPED' if scan.SHUTDOWN_REQUESTED.is_set() else 'FAILED', error=str(exc))
    finally:
        for option in ('-k', '-w'):
            try:
                subprocess.run(['wineserver', option], env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=15, check=False)
            except (OSError, subprocess.TimeoutExpired): pass
        for name, path in scan.collect_runtime_outputs(runtime).items(): shutil.copy2(path, attempt / name)
        for name in ('TRIMAUTO', '.trim-progress.json', '.trim-process.log', '.xvfb.log'):
            if (runtime / name).exists(): shutil.copy2(runtime / name, attempt / name)
        shutil.copy2(runtime / 'Data/TRIM.cfg', attempt / 'TRIM.cfg')
        result['artifact_sha256'] = {str(p): scan.sha256_file(p) for p in sorted(attempt.iterdir()) if p.is_file()}
        result['wall_seconds'] = time.monotonic() - started
        write_json(directory / 'result.json', result)
    return result


def publish(state, results):
    write_json(RUN / 'status.json', state); write_json(RUN / 'results.json', results)
    columns = ['name', 'status', 'geometry', 'pressure_atm', 'ionization_fraction', 'reported_range_summary_mm',
               'stopped_ion_mean_dx_mm', 'endpoint_count', 'backscattered_ions', 'transmitted_ions']
    atomic_text(RUN / 'summary.csv', csv_text(results, columns))
    body = '''<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30">
<title>Interior-start geometry feasibility</title><style>body{font:16px system-ui;margin:30px}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>C2a: TRIM.DAT interior-start feasibility — production held</h1>
<p>28 fixed 50-ion cases, 100-keV F in Kr80/CF4-20 at 0.5/1/3 atm, three reused seeds.
Two canaries gate mode-5 near-surface, central and deeper-central starts. Binding parameters unchanged;
geometry is a separate candidate, never pooled with production. This is not an independent precision test.</p>
<p>Full primary endpoints are retained. Reported RANGE.TXT summaries and endpoint-minus-start
means are separate; backscattered/transmitted ions are counted explicitly. Interior cases require
all 50 stopped primary endpoints and no primary escape. Wider geometry tests finite-boundary sensitivity;
no universal containment or detector-quenching claim is implied.</p>
<p>15 cores maximum, 24/32 GiB RAM, no scan swap, three cumulative hours.
<a href="plan.json">Frozen plan and prior review</a> · <a href="results.json">Results/raw paths</a> ·
<a href="summary.csv">CSV</a> · <a href="logs/campaign.log">Log</a> ·
<a href="https://www.srim.org/SRIM/SRIM%2008.pdf">SRIM input manual (8-14/8-15)</a></p>'''
    body += table([state], list(state))
    if results: body += table(results, columns)
    atomic_text(RUN / 'index.html', body)


def execute():
    group = Path(resource_sample()['cgroup'])
    if (group.name != UNIT or len(os.sched_getaffinity(0)) > 15 or
            (group / 'memory.max').read_text().strip() != str(32 * GIB) or
            (group / 'memory.swap.max').read_text().strip() != '0'):
        raise RuntimeError('Use bulk_geometry_probe.sh for enforced limits')
    used = json.loads((RUN / 'budget.json').read_text())['used_seconds'] if (RUN / 'budget.json').exists() else 0.
    if used >= BUDGET or memory_info()['MemAvailable'] < 16 * GIB: raise RuntimeError('Insufficient memory/time budget')
    start = time.monotonic(); deadline = start + BUDGET - used
    state = dict(status='RUNNING', started_at=datetime.now().astimezone().isoformat(), phase='Preparing frozen canaries')
    results = []
    def stop(sig, frame): scan.SHUTDOWN_REQUESTED.set()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    def update():
        elapsed = used + time.monotonic() - start
        if elapsed >= BUDGET: scan.SHUTDOWN_REQUESTED.set()
        state.update(updated_at=datetime.now().astimezone().isoformat(), completed_cases=sum(r['status'] == 'COMPLETE' for r in results),
                     expected_cases=28, cumulative_hours=elapsed / 3600, resources=resource_sample())
        write_json(RUN / 'budget.json', dict(used_seconds=elapsed, limit_seconds=BUDGET)); publish(state, results)
    try:
        update(); plan, inputs = prepare(RUN)
        cfg = scan.load_config(d.CONFIG); context = scan.resolve_execution_context(cfg)
        runtimes, _ = scan.prepare_runtimes(scan.resolve_srim_root(None, cfg), RUN, 15, context)
        write_json(RUN / f'session-{os.getpid()}.json', dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                                                          cpu_affinity=sorted(os.sched_getaffinity(0))))
        for phase in ('canary', 'variants'):
            state['phase'] = phase; pending = []
            for case in plan['cases']:
                if case['phase'] != phase: continue
                result = cached(case)
                if result: results.append(result)
                else: pending.append(case)
            with ThreadPoolExecutor(max_workers=15) as pool:
                available, active = list(runtimes), {}
                while pending or active:
                    while pending and available and not scan.SHUTDOWN_REQUESTED.is_set():
                        if deadline - time.monotonic() < 1845:
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
                        if result['status'] != 'COMPLETE': scan.SHUTDOWN_REQUESTED.set()
                    update()
            if any(r['status'] == 'FAILED' for r in results): raise RuntimeError('Case failed; further release stopped')
            if scan.SHUTDOWN_REQUESTED.is_set(): raise InterruptedError('Stopped or cumulative budget insufficient')
            if phase == 'canary' and (len(results) != 2 or any(r['status'] != 'COMPLETE' for r in results)):
                raise RuntimeError('Both canaries must pass before variants')
            update()
        prepare(RUN)
        if len(results) != 28 or int(dict(line.split() for line in (group / 'memory.events').read_text().splitlines()).get('oom_kill', 0)):
            raise RuntimeError('Count or OOM check failed')
        state.update(status='REVIEW_REQUIRED', phase='Geometry feasibility complete; no production release', source_unchanged=True)
        update(); write_json(RUN / 'completion.json', state); return 0
    except Exception as exc:
        state.update(status='STOPPED' if isinstance(exc, InterruptedError) else 'NEEDS_ATTENTION', error=str(exc))
        update(); print(str(exc), file=sys.stderr, flush=True); return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'worker', 'status', 'review'))
    parser.add_argument('--runtime', type=Path); parser.add_argument('--name')
    args = parser.parse_args()
    if args.command == 'worker':
        d.pysrim_runner.configure_recoil_capacity(args.runtime)
        d.pysrim_runner.monitored_trim_run(d.ArchivedInput(), args.runtime, dict(description=args.name)); return 0
    if args.command == 'review':
        review, _ = review_previous(); write_json(d.RUN / 'review/summary.json', review)
        print(json.dumps(review, indent=2)); return 0
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN / 'status.json').exists(): print((RUN / 'status.json').read_text())
        print('Report:', RUN / 'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, _ = prepare(RUN)
            print(json.dumps({k: plan[k] for k in ('model_id', 'total_cases', 'diagnostic_histories', 'planning_minutes', 'budget_hours', 'design')}, indent=2)); return 0
        return execute()


if __name__ == '__main__': raise SystemExit(main())
