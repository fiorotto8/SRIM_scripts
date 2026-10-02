#!/usr/bin/env python3
"""Audit P2a raw results and describe them without claiming batch precision."""
from collections import Counter
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import high_energy_pressure as prior
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json

RUN = prior.RUN
b, scan = prior.b, prior.scan


def baseline_rows():
    # Called only after prior.prepare has re-audited the complete baseline.
    pilot = json.loads((b.p.RUN / 'results.json').read_text())
    rebuild = json.loads((prior.previous.RUN / 'results.json').read_text())
    return b.production_rows(pilot, rebuild)


def review():
    plan, inputs = prior.prepare(RUN)
    completion = json.loads((RUN / 'completion.json').read_text())
    if completion['status'] != 'REVIEW_REQUIRED' or not completion['source_unchanged']:
        raise ValueError('P2a is incomplete')
    rows = json.loads((RUN / 'results.json').read_text())
    indexed = {row['name']: row for row in rows}
    if len(rows) != 18 or len(indexed) != 18 or set(indexed) != {c['name'] for c in plan['cases']}:
        raise ValueError('P2a count or identity mismatch')
    for case in plan['cases']:
        row = indexed[case['name']]
        if row != json.loads((RUN / 'cases' / case['name'] / 'result.json').read_text()):
            raise ValueError('P2a result index changed')
        prior.audit_case(case, row, inputs[case['name']])
    baseline = baseline_rows()
    if len(baseline) != 972 or len({r['seed'] for r in baseline + rows}) != 990:
        raise ValueError('P2a independent seed accounting mismatch')
    points = b.p.point_summary(baseline)
    refs = {b.key(p): p for p in points}
    descriptive = []
    for row in sorted(rows, key=b.key):
        reference = refs[row['family'], row['ion'], 1000., 80, 1.]
        item = {k: row[k] for k in ('point', 'family', 'ion', 'energy_kev', 'noble_percent',
                                    'pressure_atm', 'ionization_fraction', 'stopped_ion_mean_dx_mm', 'wall_seconds')}
        item.update(ions=50, batches=1, independent_batch_sem=None,
                    pressure_scaled_range_ratio=row['stopped_ion_mean_dx_mm'] * row['pressure_atm'] /
                                                reference['projected_range_mm'],
                    ionization_ratio=row['ionization_fraction'] / reference['ionization_fraction'])
        descriptive.append(item)
    costs = {(row['family'], row['ion'], row['pressure_atm']): row['wall_seconds'] for row in rows}
    ratios = [costs[f, ion, .5] / costs[f, ion, 3.] for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)]
    summary = dict(status='AUDITED_READY_FOR_FIXED_P2B', model_id=prior.MODEL,
                   complete_points=18, independent_batches=18, histories=900,
                   actual_hours=completion['cumulative_hours'], resources=completion['resources'],
                   longest_batch_seconds=max(row['wall_seconds'] for row in rows),
                   lower_pressure_cost_ratio_min=min(ratios), lower_pressure_cost_ratio_max=max(ratios),
                   max_descriptive_scaled_range_relative_difference=max(abs(p['pressure_scaled_range_ratio'] - 1) for p in descriptive),
                   max_descriptive_ionization_relative_difference=max(abs(p['ionization_ratio'] - 1) for p in descriptive),
                   recovered_monitor_races=sum(x['status'] == 'recovered' for r in rows
                                              for x in r['completion_monitor']['inspections']),
                   pressure_screen=plan['pressure_screen_after_p2b'],
                   decision='All eighteen cases pass raw hashes, actual species/energy/bindings/density, fifty contained '
                            'endpoints, zero escape/edge signal, normal completion and authenticated display gates. '
                            'One batch per new point has no independent-batch SEM and cannot establish pressure equivalence. '
                            'Proceed only to P2b: three additional reserved independent 50-ion batches per point, '
                            'then report the already frozen 36 pressure intervals and precision flags. '
                            'No automatic mixture expansion or repeated precision sampling.')
    return summary, rows, descriptive, baseline, plan


def plots(rows, baseline, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    refs = {b.key(p): p for p in b.p.point_summary(baseline)}
    values = {b.key(r): r for r in rows}
    fig, axes = plt.subplots(3, 2, figsize=(11, 12), squeeze=False)
    for i, family in enumerate(('Ar', 'Kr', 'Xe')):
        for ion in ('C', 'F', family):
            reference = refs[family, ion, 1000., 80, 1.]
            rr = [values[family, ion, 1000., 80, p] for p in (.5, 3.)]
            for j, metric in enumerate(('projected_range_mm', 'ionization_fraction')):
                field = 'stopped_ion_mean_dx_mm' if j == 0 else metric
                y = [rr[0][field] * (.5 if j == 0 else 1), reference[metric],
                     rr[1][field] * (3 if j == 0 else 1)]
                ax = axes[i, j]
                ax.plot([.5, 1, 3], y, 'o-', label=ion)
                ax.errorbar([1], [y[1]], yerr=[reference[metric + '_sem']], capsize=3)
                ax.set_xlabel('Pressure [atm]'); ax.set_xticks([.5, 1, 3]); ax.grid(True, alpha=.3); ax.legend()
                ax.set_title(f'{family}80/CF4-20, 1 MeV')
                ax.set_ylabel('Pressure × displacement [atm mm]' if j == 0 else 'SRIM ionization fraction')
    fig.suptitle('P2a: 50 ions at 0.5/3 atm; 200 ions at 1 atm\n'
                 'Error bars only at 1 atm: batch SEM. Other points have no batch SEM; descriptive only.')
    fig.tight_layout(); fig.savefig(out / 'pressure.pdf'); fig.savefig(out / 'pressure.png', dpi=110); plt.close(fig)


def main():
    summary, rows, descriptive, baseline, _ = review(); out = RUN / 'review'
    write_json(out / 'summary.json', summary)
    atomic_text(out / 'points.csv', csv_text(descriptive, list(descriptive[0])))
    plots(rows, baseline, out)
    body = '<html><meta charset="utf-8"><title>P2a raw audit</title><h1>Audited 1-MeV pressure timing gate</h1>'
    body += '<p>' + summary['decision'] + '</p><p><a href="pressure.pdf">PDF</a> · <a href="points.csv">Descriptive CSV</a></p>'
    body += table([summary], list(summary)) + '<img src="pressure.png" style="max-width:100%">'
    body += table(descriptive, list(descriptive[0])); atomic_text(out / 'index.html', body)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
