#!/usr/bin/env python3
"""Cold audit of the fixed M3 cohort; descriptive trends, not feature claims."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixtures_1atm_noreset as prior
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json

p, b, scan = prior.p, prior.p.b, prior.p.scan
RUN = prior.RUN


def review():
    plan, inputs, baseline = prior.prepare(RUN)
    prior.gate.verify_hashes(plan['input_sha256'])
    state = json.loads((RUN/'completion.json').read_text())
    if (state != json.loads((RUN/'status.json').read_text()) or state['status'] != 'REVIEW_REQUIRED'
            or not state['source_unchanged'] or state['completed_new_batches'] != 1260
            or state['retained_original_batches'] != 401 or state['completed_continuation_batches'] != 859):
        raise ValueError('M3 completion/accounting mismatch')
    rows = json.loads((RUN/'results.json').read_text()); indexed = {r['name']: r for r in rows}
    if len(rows) != 1260 or set(indexed) != {c['name'] for c in plan['cases']}:
        raise ValueError('M3 cohort mismatch')
    with prior.r.parser_context():
        for case in plan['cases']:
            row = indexed[case['name']]
            if row != json.loads((RUN/'cases'/case['name']/'result.json').read_text()) or row['diagnostic']:
                raise ValueError('Production row changed or diagnostic pooled')
            prior.audit_case(case, row, inputs[case['name']])
    combined = baseline+rows; points = b.p.point_summary(combined)
    if (len(combined) != 2304 or len({r['seed'] for r in combined}) != 2304 or len(points) != 576
            or not all(x['complete'] for x in points) or sum(x['pressure_atm'] == 1 for x in points) != 504):
        raise ValueError('Combined mixture accounting changed')
    if points != json.loads((RUN/'points.json').read_text()):
        raise ValueError('Combined point statistics changed')
    groups = defaultdict(list)
    for row in combined:
        groups[b.key(row)].append(row['wall_seconds'])
    timing = [dict(family=k[0], ion=k[1], energy_kev=k[2], noble_percent=k[3], pressure_atm=k[4],
                   batches=len(v), mean_seconds=statistics.mean(v), max_seconds=max(v)) for k, v in sorted(groups.items())]
    flags = [x for x in points if not x['precision_met']]
    refs = {b.key(x): x for x in points}; trends = []
    for family in ('Ar', 'Kr', 'Xe'):
        for ion in ('C', 'F', family):
            for energy in p.ENERGIES:
                a, z = refs[family, ion, energy, 60, 1.], refs[family, ion, energy, 95, 1.]
                trends.append(dict(family=family, ion=ion, energy_kev=energy,
                                   displacement_ratio_95_to_60=z['projected_range_mm']/a['projected_range_mm'],
                                   ionization_ratio_95_to_60=z['ionization_fraction']/a['ionization_fraction'],
                                   both_endpoints_meet_precision=a['precision_met'] and z['precision_met']))
    summary = dict(status='AUDITED_READY_FOR_P4', complete_points=576, complete_1atm_points=504,
                   independent_batches=2304, histories=115200, points_meeting_precision=576-len(flags),
                   precision_flags=len(flags), new_mixture_flags=315-state['new_points_meeting_precision'],
                   range_flags=sum(x['projected_range_mm_relative_sem'] > .05 for x in points),
                   ionization_flags=sum(x['ionization_fraction_relative_sem'] > .02 for x in points),
                   flags_by_energy={f'{e:g}': count for e, count in sorted(Counter(x['energy_kev'] for x in flags).items())},
                   flags_by_family=dict(Counter(x['family'] for x in flags)),
                   max_range_relative_sem=max(x['projected_range_mm_relative_sem'] for x in points),
                   max_ionization_relative_sem=max(x['ionization_fraction_relative_sem'] for x in points),
                   cumulative_m3_hours=state['cumulative_hours'],
                   last_continuation_hours=state['cumulative_hours']-plan['inherited_used_seconds']/3600,
                   resources=state['resources'],
                   recovered_completion_probes=sum(x['status'] == 'recovered' for row in rows for x in row['completion_monitor']['inspections']),
                   diagnostic_histories_excluded=100,
                   decision='All1260 M3 batches passed full raw/input/parameter/containment/completion/source audits. '
                            'All576 combined points have four independent50-ion batches. Preserve99precision flags '
                            '(46prior+53new) for fixed Q6 follow-ups; no adaptive topups or feature-significance claims. '
                            'Mixture endpoint ratios are descriptive,not independent confirmations or detector quenching. '
                            'Proceed only to fixed P4 direct80%-noble pressure/energy completion; G5 remains callback-gated.')
    return summary, combined, points, timing, trends


def main():
    summary, rows, points, timing, trends = review(); out = RUN/'review'
    write_json(out/'summary.json', summary)
    for name, data in [('points', points), ('timing', timing), ('mixture_trends', trends),
                       ('precision_flags', [x for x in points if not x['precision_met']])]:
        atomic_text(out/(name+'.csv'), csv_text(data, sorted({k for x in data for k in x})))
    body = '<html><meta charset="utf-8"><h1>Audited Ar/Kr/Xe mixture scan</h1><p>'+summary['decision']+'</p>'
    body += '<p><a href="../mixtures.pdf">Ten-page mixture PDF</a> · <a href="points.csv">576 points and SEM</a> · '
    body += '<a href="precision_flags.csv">99 precision flags</a> · <a href="timing.csv">Measured times</a> · '
    body += '<a href="mixture_trends.csv">Descriptive 95%/60% endpoint ratios</a></p>'
    atomic_text(out/'index.html', body+table([summary], list(summary)))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
