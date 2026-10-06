#!/usr/bin/env python3
"""Cold P4 raw audit and measured timing handoff; never launch simulations."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import pressure_grid as g
from scripts.preliminary_report import atomic_text, csv_text, table
from scripts.staged_scan import write_json

RUN = g.RUN
p, b, scan = g.p, g.b, g.scan


def review():
    plan, inputs, baseline = g.prepare(RUN)
    g.gate.verify_hashes(plan['input_sha256'])
    state = json.loads((RUN/'completion.json').read_text())
    if (state != json.loads((RUN/'status.json').read_text()) or state['status'] != 'REVIEW_REQUIRED'
            or not state['source_unchanged'] or state['completed_batches'] != 972
            or not all(state[phase+'_passed'] for phase in g.PHASES)):
        raise ValueError('P4 completion/accounting mismatch')
    rows = json.loads((RUN/'results.json').read_text()); indexed = {r['name']: r for r in rows}
    if len(rows) != 972 or set(indexed) != {c['name'] for c in plan['cases']}:
        raise ValueError('P4 cohort mismatch')
    with g.gate.retry.parser_context():
        for case in plan['cases']:
            row = indexed[case['name']]
            if row != json.loads((RUN/'cases'/case['name']/'result.json').read_text()) or row['diagnostic']:
                raise ValueError('P4 production row changed or diagnostic pooled')
            g.audit_case(case, row, inputs[case['name']])
    combined = baseline+rows; points = b.p.point_summary(combined)
    if (len(combined) != 3276 or len({r['seed'] for r in combined}) != 3276 or len(points) != 819
            or not all(x['complete'] for x in points) or any(r.get('diagnostic', False) for r in combined)):
        raise ValueError('Combined P4 point/seed accounting changed')
    if points != json.loads((RUN/'points.json').read_text()):
        raise ValueError('P4 statistics changed')
    checks = g.pressure_checks(combined, plan['pressure_screen'])
    if checks != json.loads((RUN/'pressure_checks.json').read_text()):
        raise ValueError('P4 pressure screening changed')
    classes = dict(Counter(x['classification'] for x in checks))
    if classes.get('followup', 0):
        raise ValueError('P4 pressure follow-up required before G5')
    groups = defaultdict(list)
    for row in combined:
        groups[b.key(row)].append(row['wall_seconds'])
    timing = [dict(family=k[0], ion=k[1], energy_kev=k[2], noble_percent=k[3], pressure_atm=k[4],
                   batches=len(v), mean_seconds=statistics.mean(v), max_seconds=max(v)) for k, v in sorted(groups.items())]
    flags = [x for x in points if not x['precision_met']]
    summary = dict(status='AUDITED_READY_FOR_G5_60', complete_points=819, independent_batches=3276,
                   histories=163800, points_meeting_precision=819-len(flags), precision_flags=len(flags),
                   range_flags=sum(x['projected_range_mm_relative_sem'] > .05 for x in points),
                   ionization_flags=sum(x['ionization_fraction_relative_sem'] > .02 for x in points),
                   flags_by_energy={f'{e:g}': n for e, n in sorted(Counter(x['energy_kev'] for x in flags).items())},
                   flags_by_family=dict(Counter(x['family'] for x in flags)), pressure_classes=classes,
                   actual_hours=state['cumulative_hours'], resources=state['resources'],
                   decision='All972 P4 cases passed full raw/input/parameter/containment/completion/source audits. '
                            'All819 combined points have four independent50-ion batches. Retain141 precision flags '
                            'for fixed Q6 cohorts. Pressure screening has28 within margins,602 inconclusive,zero outside: '
                            'not pressure equivalence. Release only fixed G5-60; manual callback before G5-65. '
                            'Ionization fraction is not detector-calibrated quenching.')
    return summary, combined, points, timing, checks


def main():
    summary, _, points, timing, checks = review(); out = RUN/'review'
    write_json(out/'summary.json', summary)
    for name, data in [('points', points), ('timing', timing), ('pressure_checks', checks),
                       ('precision_flags', [x for x in points if not x['precision_met']])]:
        atomic_text(out/(name+'.csv'), csv_text(data, sorted({k for x in data for k in x})))
    atomic_text(out/'index.html', '<html><meta charset="utf-8"><h1>Audited P4 pressure grid</h1><p>'+
                summary['decision']+'</p><p><a href="../pressure.pdf">Pressure plots</a> · '
                '<a href="points.csv">819 points and SEM</a> · <a href="precision_flags.csv">Precision flags</a></p>'+
                table([summary], list(summary)))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
