#!/usr/bin/env python3
"""Render the manual-gated roadmap and measured runtime estimates; never launch jobs."""
from __future__ import annotations
import csv
from collections import defaultdict
from datetime import datetime
import hashlib
import heapq
import html
from itertools import product
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.preliminary_report import atomic_text, csv_text, table

PLAN = ROOT / 'configs/campaign_roadmap.json'
TIMINGS = ROOT / 'runs/noble_cf4_corrected_closeout/report/batches.csv'
OUT = ROOT / 'runs/campaign_schedule'


def grid(target):
    return {(family, ion, float(energy), noble, float(pressure))
            for family, noble, pressure, energy in product(
                target['families'], target['noble_percent'], target['pressures_atm'], target['energies_kev'])
            for ion in ('C', 'F', family)}


def existing_grid(target):
    return {key for key in grid(target) if (key[3] in (60, 80, 95) and key[4] == 1)
            or (key[3] == 80 and key[4] in (.5, 3) and key[2] in (1, 10, 100))}


def selections(target):
    full, old = grid(target), existing_grid(target)
    high = {k for k in full - old if k[3] == 80 and k[4] in (.5, 3) and k[2] == 1000}
    mixture = {k for k in full - old if k[4] == 1}
    pressure = {k for k in full - old - high - mixture if k[3] == 80}
    cross = full - old - high - mixture - pressure
    if (len(full), len(old), len(high), len(mixture), len(pressure), len(cross)) != (3024, 243, 18, 315, 243, 2205):
        raise ValueError('Roadmap grid changed; review stage definitions and forecasts')
    return dict(high_energy_pressure=high, mixtures_1atm=mixture,
                pressure_80_remaining=pressure, cross_grid=cross)


def timing_means(path):
    grouped, ids, seeds = defaultdict(list), set(), set()
    with path.open(newline='') as handle:
        for row in csv.DictReader(handle):
            if row['job_id'] in ids or row['random_seed'] in seeds:
                raise ValueError('Duplicate timing records')
            ids.add(row['job_id']); seeds.add(row['random_seed'])
            key = (row['family'], row['ion'], float(row['energy_kev']),
                   int(row['noble_percent']), float(row['pressure_atm']))
            if int(row['n_ions']) != 50 or float(row['wall_seconds_last_attempt']) <= 0:
                raise ValueError('Expected measured positive 50-ion batch durations')
            grouped[key].append(float(row['wall_seconds_last_attempt']))
    if len(ids) != 1841 or len(grouped) != 243:
        raise ValueError('Expected current 1841-batch / 243-point timing reference')
    return {k: statistics.mean(v) for k, v in grouped.items()}


def interpolate(x, a, b, ya, yb):
    return ya + (yb - ya) * (x - a) / (b - a)


def batch_cost(key, means):
    if key in means:
        return means[key]
    family, ion, energy, noble, pressure = key
    a, b = (60, 80) if noble <= 80 else (80, 95)
    base = interpolate(noble, a, b, means[family, ion, energy, a, 1], means[family, ion, energy, b, 1])
    def factor(p):
        # Conservative observed low/mid-energy ratio for this family/recoil.
        return max(means[family, ion, e, 80, p] / means[family, ion, e, 80, 1] for e in (1, 10, 100))
    ratio = 1 if pressure == 1 else factor(.5) if pressure == .5 else interpolate(pressure, 1, 3, 1, factor(3))
    return base * ratio


def estimate(keys, batches, means, workers=15):
    if not keys or batches < 1 or workers < 1:
        raise ValueError('Expected non-empty work and positive batch/worker counts')
    costs = sorted((batch_cost(k, means) for k in keys for _ in range(batches)), reverse=True)
    loads = [0.] * workers
    for cost in costs:
        heapq.heapreplace(loads, loads[0] + cost)
    model = max(loads) / 3600
    return dict(points=len(keys), batches=len(costs), histories=50 * len(costs),
                model_hours=round(model, 2), planning_hours=round(1.5 * model + .5, 2),
                longest_batch_hours=round(costs[0] / 3600, 2))


def main():
    plan = json.loads(PLAN.read_text())
    choices = selections(plan['target'])
    means = timing_means(TIMINGS)
    if set(means) != existing_grid(plan['target']):
        raise ValueError('Timing coverage differs from reviewed baseline')
    forecasts = []
    for stage in plan['stages']:
        if 'selection' not in stage:
            continue
        selected = choices[stage['selection']]
        waves = stage.get('wave_noble_percent', [None])
        for wave in waves:
            keys = {k for k in selected if wave is None or k[3] == wave}
            row = dict(id=stage['id'] + (f'-{wave}' if wave is not None else ''),
                       title=stage['title'], **estimate(keys, stage['batches_per_point'], means))
            row['cap_hours'] = stage.get('cap_hours', stage.get('cap_hours_per_wave'))
            row['fits_proposed_cap'] = row['planning_hours'] <= row['cap_hours']
            forecasts.append(row)
    diagnostic = ROOT / 'runs/kr_pressure_diagnostic/status.json'
    totals = dict(remaining_baseline_new_points=2781,
                  remaining_baseline_batches=sum(r['batches'] for r in forecasts),
                  remaining_baseline_histories=sum(r['histories'] for r in forecasts),
                  sequential_model_hours=round(sum(r['model_hours'] for r in forecasts), 2),
                  sequential_planning_hours=round(sum(r['planning_hours'] for r in forecasts), 2))
    summary = dict(generated_at=datetime.now().astimezone().isoformat(),
                   note='Planning schedule only; no timer, background automation or simulation is installed/launched.',
                   input_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (PLAN, TIMINGS, Path(__file__))},
                   timing_method='Measured mean 50-ion wall times; linear mixture interpolation between '
                   '60/80/95% at 1 atm. Pressure multiplier is the largest observed same-species '
                   'ratio over 1/10/100 keV; linearly interpolate 1 to 3 atm. '
                   'This extrapolates pressure costs to 1 MeV and must be replaced by P2 measurements. '
                   '15-worker longest-first scheduling, 50% margin plus 30 minutes per stage/wave. '
                   'Planning estimates are not statistical confidence bounds or reserved calendar slots.',
                   assumptions='Current accepted model and retained 243 points remain reusable. '
                   'No estimate includes time waiting for user continue, model-change reruns or unplanned fixes. '
                   'Core/RAM contention may require fewer workers and a revised forecast.',
                   baseline_totals=totals, stages=forecasts,
                   current_diagnostic=json.loads(diagnostic.read_text()) if diagnostic.exists() else None)
    atomic_text(OUT / 'forecast.json', json.dumps(summary, indent=2) + '\n')
    atomic_text(OUT / 'baseline_schedule.csv', csv_text(forecasts, list(forecasts[0])))
    body = '''<!doctype html><meta charset="utf-8"><title>SRIM gated campaign schedule</title>
<style>body{font:16px system-ui;max-width:1300px;margin:30px auto;padding:16px}table{border-collapse:collapse}td,th{padding:7px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>SRIM campaign: one review gate per “continue”</h1>
<p>Target: 3,024 direct points, Ar/Kr/Xe, C/F/noble recoils, eight mixtures,
six pressures, seven energies from 1 keV to 1 MeV. SRIM ionization fraction
is not detector-calibrated quenching. No unattended stage chaining is enabled.</p>
<p><a href="../../configs/campaign_roadmap.json">Full roadmap and contingencies</a> ·
<a href="forecast.json">Timing assumptions and live-status snapshot</a> ·
<a href="baseline_schedule.csv">Baseline schedule CSV</a> ·
<a href="../kr_pressure_diagnostic/index.html">Current diagnostic</a> ·
<a href="../noble_cf4_corrected_closeout/review/index.html">Completed preliminary analysis</a></p>'''
    body += '<p>Snapshot: ' + html.escape(summary['generated_at']) + '</p>'
    body += '<p>' + html.escape(summary['timing_method']) + '</p>'
    body += table([totals], list(totals))
    body += '<h2>Compute-only baseline stages (each awaits continue)</h2>'
    body += table(forecasts, list(forecasts[0]))
    body += '<h2>Stage order, work and release criteria</h2>'
    for stage in plan['stages']:
        body += '<h3>' + html.escape(stage['id'] + ' — ' + stage['title']) + '</h3>'
        body += '<p>' + html.escape(stage['scope']) + '</p><p>Gate: ' + html.escape(stage['exit_gate']) + '</p>'
        for field in ('expected_compute_hours', 'expected_active_work_hours'):
            if field in stage:
                body += '<p>' + html.escape(field + ': ' + str(stage[field])) + '</p>'
    body += '<h2>Conditional interventions — insert before dependent work</h2>'
    for item in plan['contingencies']:
        body += '<h3>' + html.escape(item['id'] + ' — ' + item['trigger']) + '</h3>'
        body += '<p>' + html.escape(item['action']) + '</p><p>' + html.escape(item['uncertainty']) + '</p>'
        body += '<p>Compute hours: ' + html.escape(str(item['expected_compute_hours'])) + '</p>'
        if 'expected_active_work_hours' in item:
            body += '<p>Active work hours: ' + html.escape(str(item['expected_active_work_hours'])) + '</p>'
    body += '<h2>Continue protocol</h2><ol>' + ''.join('<li>' + html.escape(s) + '</li>' for s in plan['continue_protocol']) + '</ol>'
    body += '<h2>Done criteria</h2><ol>' + ''.join('<li>' + html.escape(s) + '</li>' for s in plan['done_criteria']) + '</ol>'
    atomic_text(OUT / 'index.html', body)
    print(json.dumps(totals, indent=2))
    for row in forecasts:
        print(f"{row['id']}: {row['points']} points, {row['batches']} batches, planning {row['planning_hours']} h, cap {row['cap_hours']} h")
    print('Report:', OUT / 'index.html')


if __name__ == '__main__':
    main()
