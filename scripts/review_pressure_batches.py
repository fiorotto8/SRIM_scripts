#!/usr/bin/env python3
"""Audit P2b independence, precision, pressure intervals and production release."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import high_energy_pressure_batches as prior
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json
from scripts.historical_execution import historical_sources

RUN = prior.RUN
b, scan = prior.b, prior.scan


def review():
    with historical_sources() as environment:
        plan, inputs, retained, baseline = prior.prepare(RUN)
    completion = json.loads((RUN / 'completion.json').read_text())
    if completion['status'] != 'REVIEW_REQUIRED' or not completion['source_unchanged']:
        raise ValueError('P2b incomplete')
    rows = json.loads((RUN / 'results.json').read_text()); indexed = {r['name']: r for r in rows}
    if len(rows) != 54 or len(indexed) != 54 or set(indexed) != {c['name'] for c in plan['cases']}:
        raise ValueError('P2b case count or identity mismatch')
    for case in plan['cases']:
        row = indexed[case['name']]
        if row != json.loads((RUN / 'cases' / case['name'] / 'result.json').read_text()):
            raise ValueError('P2b result index changed')
        prior.prior.audit_case(case, row, inputs[case['name']])
        if str(Path(row['attempt']) / '.completion-probes.json') not in row['artifact_sha256']:
            raise ValueError('Missing completion-monitor raw hash')
    pressure_rows = retained + rows; points = b.p.point_summary(pressure_rows)
    if len(points) != 18 or not all(p['complete'] for p in points):
        raise ValueError('Incomplete four-batch pressure points')
    if points != json.loads((RUN / 'points.json').read_text()):
        raise ValueError('Pressure point statistics changed')
    checks = prior.pressure_checks(pressure_rows, baseline, plan['pressure_screen'])
    if len(checks) != 36 or checks != json.loads((RUN / 'pressure_checks.json').read_text()):
        raise ValueError('Prespecified pressure intervals changed')
    combined = baseline + pressure_rows; all_points = b.p.point_summary(combined)
    if len(combined) != 1044 or len({r['seed'] for r in combined}) != 1044 or len(all_points) != 261 or not all(p['complete'] for p in all_points):
        raise ValueError('Combined independent production accounting changed')
    groups = defaultdict(list)
    for row in combined:
        groups[b.key(row)].append(row['wall_seconds'])
    timing = [dict(family=k[0], ion=k[1], energy_kev=k[2], noble_percent=k[3], pressure_atm=k[4],
                   batches=4, mean_seconds=statistics.mean(v), max_seconds=max(v)) for k, v in sorted(groups.items())]
    classifications = dict(Counter(c['classification'] for c in checks))
    summary = dict(status='SCIENTIFIC_AUDIT_PASSED_CURRENT_ENVIRONMENT_GATE_REQUIRED' if classifications.get('followup', 0) == 0 else 'HOLD_PRESSURE_FEATURE_REVIEW',
                   model_id=prior.MODEL, complete_pressure_points=18, pressure_batches=72, pressure_histories=3600,
                   new_batches=54, independent_production_points=261, independent_production_batches=1044,
                   independent_production_histories=52200, pressure_points_meeting_precision=sum(p['precision_met'] for p in points),
                   all_points_meeting_precision=sum(p['precision_met'] for p in all_points),
                   all_precision_flags=sum(not p['precision_met'] for p in all_points),
                   max_pressure_range_relative_sem=max(p['projected_range_mm_relative_sem'] for p in points),
                   max_pressure_ionization_relative_sem=max(p['ionization_fraction_relative_sem'] for p in points),
                   pressure_classifications=classifications,
                   pressure_classifications_by_metric={metric: dict(Counter(c['classification'] for c in checks if c['metric'] == metric))
                                                       for metric in ('ionization_fraction', 'stopped_ion_mean_dx_mm')},
                   actual_hours=completion['cumulative_hours'], resources=completion['resources'],
                   longest_batch_seconds=max(r['wall_seconds'] for r in rows),
                   recovered_monitor_races=sum(x['status'] == 'recovered' for r in rows for x in r['completion_monitor']['inspections']),
                   environment_transition=environment,
                   decision='All independent cases and the frozen 36-comparison pressure screen are re-audited. '
                            'Seventeen intervals are inside working margins, nineteen inconclusive, none entirely outside. '
                            'This is not global pressure equivalence; four-batch SEM is estimated sampling precision, not model accuracy. '
                            'There is no identified critical integrity/containment or outside-margin pressure feature. '
                            'The OS upgraded Xvfb after P2b finished; validate the current display environment and exact replay '
                            'before releasing M3 direct 1-atm mixture simulations. Historical Xvfb bytes were recovered '
                            'hash-identically for the historical audit; no frozen plan or raw output is edited. '
                            'Keep every pressure point direct; do not replace unmeasured '
                            'pressures with scaled physics results. Retain 46 earlier precision flags for fixed Q6 cohorts. '
                            'No automatic later stage, extra histories or detector-quenching claim.')
    return summary, combined, all_points, timing, checks


def plots(points, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    refs = {b.key(p): p for p in points}
    fig, axes = plt.subplots(3, 2, figsize=(11, 12), squeeze=False)
    for i, family in enumerate(('Ar', 'Kr', 'Xe')):
        for ion in ('C', 'F', family):
            rr = [refs[family, ion, 1000., 80, p] for p in (.5, 1., 3.)]
            for j, metric in enumerate(('projected_range_mm', 'ionization_fraction')):
                scales = [.5, 1., 3.] if j == 0 else [1., 1., 1.]
                axes[i, j].errorbar([.5, 1., 3.], [r[metric]*s for r, s in zip(rr, scales)],
                                   yerr=[r[metric+'_sem']*s for r, s in zip(rr, scales)], marker='o', capsize=3, label=ion)
                axes[i, j].set_title(f'{family}80/CF4-20, 1 MeV')
                axes[i, j].set_xlabel('Pressure [atm]'); axes[i, j].set_xticks([.5, 1., 3.])
                axes[i, j].set_ylabel('Pressure × displacement [atm mm]' if j == 0 else 'SRIM ionization fraction')
                axes[i, j].grid(True, alpha=.3); axes[i, j].legend()
    fig.suptitle('Independent pressure batches: 200 ions per point; error bars = batch SEM\n'
                 'Descriptive SEM bars are not the simultaneous pressure-screen intervals.')
    fig.tight_layout(); fig.savefig(out/'pressure.pdf'); fig.savefig(out/'pressure.png', dpi=110); plt.close(fig)


def main():
    summary, rows, points, timing, checks = review(); out = RUN/'review'
    write_json(out/'summary.json', summary)
    for name, data in [('points', points), ('timing', timing), ('pressure_checks', checks),
                       ('precision_flags', [p for p in points if not p['precision_met']])]:
        atomic_text(out/(name+'.csv'), csv_text(data, sorted({k for r in data for k in r})))
    plots(points, out)
    body = '<html><meta charset="utf-8"><title>P2b audited results</title><h1>Independent pressure and precision review</h1>'
    body += '<p>'+summary['decision']+'</p><p><a href="pressure.pdf">PDF plots</a> · <a href="points.csv">261 points with SEMs</a> · '
    body += '<a href="pressure_checks.csv">36 pressure intervals</a> · <a href="timing.csv">Measured timings</a></p>'
    body += table([summary], list(summary))+'<img src="pressure.png" style="max-width:100%">'
    body += table(checks, sorted({k for c in checks for k in c})); atomic_text(out/'index.html', body)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
