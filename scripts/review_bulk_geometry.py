#!/usr/bin/env python3
"""Recompute the completed C2b diagnostic from hashed raw artifacts."""
from __future__ import annotations
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import bulk_geometry_retry as prior
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json


def review():
    plan, inputs = prior.prepare(prior.RUN)
    completion = json.loads((prior.RUN / 'completion.json').read_text())
    if completion['status'] != 'REVIEW_REQUIRED' or not completion['source_unchanged']:
        raise ValueError('Geometry retry is not complete')
    saved = json.loads((prior.RUN / 'results.json').read_text())
    if len(saved) != 28 or len({r['name'] for r in saved}) != 28:
        raise ValueError('Expected exactly 28 distinct results')
    rows = []
    for case in plan['cases']:
        r = prior.checked_result(prior.RUN / 'cases' / case['name'] / 'result.json', 'COMPLETE')
        if r != next(x for x in saved if x['name'] == case['name']): raise ValueError('Result index mismatch')
        directory = Path(r['attempt'])
        if (directory / 'TRIM.IN').read_bytes() != inputs[case['name']][0]: raise ValueError('Executed input mismatch')
        if case['geometry'] != 'mode2_surface':
            if (directory / 'TRIM.DAT').read_bytes() != inputs[case['name']][1]: raise ValueError('Event input mismatch')
            ack = json.loads((directory / 'completion-ack.json').read_text())
            if ack['status'] != 'sent' or ack['title'] != prior.completion.TITLE: raise ValueError('Completion evidence missing')
        if json.loads((directory / '.trim-progress.json').read_text())['status'] != 'complete':
            raise ValueError('Normal process exit missing')
        fresh = prior.bulk.analyse(case, {p.name.upper(): p for p in directory.iterdir()})
        if any(r[k] != v for k, v in fresh.items()): raise ValueError('Raw analysis mismatch')
        rows.append(r)
    by_key = {(r['geometry'], r['repeat'], r['pressure_atm']): r for r in rows}
    depth, surface, pressure = [], [], []
    for repeat in (100, 101, 102):
        for p in (.5, 1., 3.):
            a, b, s = (by_key[g, repeat, p] for g in ('interior', 'interior_deep', 'near_surface'))
            depth.append(dict(repeat=repeat, pressure_atm=p,
                              qf_relative_change=b['ionization_fraction'] / a['ionization_fraction'] - 1,
                              dx_relative_change=b['stopped_ion_mean_dx_mm'] / a['stopped_ion_mean_dx_mm'] - 1))
            surface.append(dict(repeat=repeat, pressure_atm=p, surface_backscattered=s['backscattered_ions'],
                                interior_backscattered=a['backscattered_ions'],
                                qf_relative_change=a['ionization_fraction'] / s['ionization_fraction'] - 1,
                                dx_relative_change=a['stopped_ion_mean_dx_mm'] / s['stopped_ion_mean_dx_mm'] - 1))
    for p in (.5, 1., 3.):
        for geometry in ('near_surface', 'interior'):
            rr = [by_key[geometry, repeat, p] for repeat in (100, 101, 102)]
            pressure.append(dict(geometry=geometry, pressure_atm=p,
                                 mean_ionization_fraction=statistics.mean(r['ionization_fraction'] for r in rr),
                                 mean_pressure_times_dx=statistics.mean(r['stopped_ion_mean_dx_mm'] * p for r in rr),
                                 primary_backscattered=sum(r['backscattered_ions'] for r in rr)))
    summary = dict(status='AUDITED_CROSS_FAMILY_ENERGY_VALIDATION_REQUIRED', cases=28,
                   mode5_completion_checks=27, interior_cases=18,
                   interior_primary_escapes=0, near_surface_primary_backscattered=sum(r['surface_backscattered'] for r in surface),
                   max_depth_qf_relative_change=max(abs(r['qf_relative_change']) for r in depth),
                   max_depth_dx_relative_change=max(abs(r['dx_relative_change']) for r in depth),
                   max_raw_summary_vs_absolute_endpoint_relative_difference=max(abs(r['reported_range_summary_mm'] / r['endpoint_mean_x_mm'] - 1) for r in rows),
                   actual_compute_hours=completion['cumulative_hours'], resources=completion['resources'],
                   production_histories_added=0,
                   interpretation='All 28 raw cases pass their frozen gates. Interior starts contain all primary ions in this selected Kr/F100keV study. '
                   'RANGE.TXT is an absolute mean endpoint coordinate, NOT range from the interior origin; use per-ion endpoint X minus start X. '
                   'Depth doubling changes are small at these cases. Surface/interior histories can diverge even with the same seed; these three reused seeds '
                   'are not independent precision or pressure-equivalence evidence. Mode-2/5 single-batch differences do not establish a transport defect. '
                   'Do not pool the surface dataset with candidate interior results or adopt the geometry from one species/energy. '
                   'Next: fixed cross-family/recoil 1keV/1MeV endpoint and depth validation, under a separate diagnostic model ID. '
                   'A full accepted interior model will require its own baseline and forecast.',
                   input_sha256={str(p): prior.scan.sha256_file(p) for p in
                                 [Path(__file__), prior.RUN / 'plan.json', prior.RUN / 'completion.json', prior.RUN / 'results.json']})
    return summary, rows, depth, surface, pressure


def main():
    summary, rows, depth, surface, pressure = review()
    out = prior.RUN / 'review'
    write_json(out / 'summary.json', summary)
    body = '<!doctype html><meta charset="utf-8"><title>C2b geometry review</title><h1>C2b audited geometry review</h1>'
    body += '<p>' + summary['interpretation'] + '</p>' + table([summary], list(summary))
    for name, data in [('depth_comparisons', depth), ('surface_comparisons', surface), ('pressure_descriptive', pressure)]:
        atomic_text(out / f'{name}.csv', csv_text(data, list(data[0])))
        body += '<h2>' + name + '</h2>' + table(data, list(data[0]))
    atomic_text(out / 'index.html', body)
    print(json.dumps(summary, indent=2)); print('Report:', out / 'index.html')


if __name__ == '__main__': main()
