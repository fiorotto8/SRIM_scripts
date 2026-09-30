#!/usr/bin/env python3
"""Offline review only: never run SRIM or modify frozen campaign artifacts."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import shlex
import statistics
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import corrected_closeout as campaign
from scripts.preliminary_report import atomic_text, csv_text, table
from trim_qf_sweep import numeric_columns_from_line


def expected_atom_density(row):
    # Ideal-gas molecules/cm3 times atoms per noble/CF4 molecule.
    return (row['pressure_atm'] * 101325 / (1.380649e-23 * row['temperature_k'])
            / 1e6 * (5 - 4 * row['noble_percent'] / 100))


def raw_check(row, inputs, tdata, ioniz):
    """Independent arithmetic check of archived inputs and differential IONIZ."""
    lines = inputs.splitlines()
    layer = next(shlex.split(line) for line in lines
                 if line.startswith('1 "' + row['condition_id'] + '"'))
    width, density = map(float, layer[2:4])
    if not math.isclose(density, row['density_g_cm3'], rel_tol=1e-12):
        raise ValueError('Archived input density differs from result')
    if not math.isclose(width, row['target_width_angstrom'], rel_tol=1e-12):
        raise ValueError('Archived target width differs from result')
    phase = next(i for i, line in enumerate(lines) if 'Target layer phases' in line)
    if lines[phase + 1].strip() != '1':
        raise ValueError('Target not configured as gas')
    bindings = campaign.scan.check_target_energies(tdata, row['target_energy_check']['expected'])
    if not bindings['passed']:
        raise ValueError('Archived binding check failed')
    match = re.search(r'Density\s*=\s*([\d.Ee+-]+) atoms/cm3', tdata)
    if match is None:
        raise ValueError('Missing reported atomic density')
    reported = float(match[1])
    expected = expected_atom_density(row)
    if 'eV /(Angstrom-Ion)' not in ioniz:
        raise ValueError('Unexpected ionization units')
    bins = [v for line in ioniz.splitlines() if (v := numeric_columns_from_line(line)) is not None]
    if len(bins) != 100 or any(b[0] <= a[0] for a, b in zip(bins, bins[1:])):
        raise ValueError('Expected 100 ordered uniform-profile bins')
    step = statistics.median(b[0] - a[0] for a, b in zip(bins, bins[1:]))
    total = sum(v[1] + v[2] for v in bins)
    qf = total * step / (1000 * row['energy_kev'])
    if not math.isclose(qf, row['qf_trim_ionization'], rel_tol=1e-12):
        raise ValueError('Archived IONIZ integral differs from result')
    # Sensitivity to rounded printed depth coordinates, not replacement data.
    endpoint_step = bins[-1][0] / len(bins)
    return dict(job_id=row['job_id'], family=row['family'], ion=row['ion'],
                noble_percent=row['noble_percent'], pressure_atm=row['pressure_atm'],
                energy_kev=row['energy_kev'], density_g_cm3=density,
                expected_atoms_cm3=expected, reported_atoms_cm3=reported,
                reported_to_expected=reported / expected,
                density_header_mismatch=not math.isclose(reported, expected, rel_tol=.002),
                endpoint_width_qf_relative_change=endpoint_step / step - 1,
                qf_reintegrated=qf, backscattered_ions=row['backscattered_ions'],
                transmitted_ions=row['transmitted_ions'],
                pressure_times_width_angstrom=row['pressure_atm'] * width)


def pressure_details(rows):
    result = []
    for p in (.5, 1., 3.):
        group = [r for r in rows if (r['family'], r['noble_percent'], r['ion'], r['energy_kev'], r['pressure_atm'])
                 == ('Kr', 80, 'F', 100., p)]
        if len(group) != 12:
            raise ValueError('Expected twelve independent Kr/F/100 batches per pressure')
        values = [r['qf_trim_ionization'] for r in group]
        result.append(dict(pressure_atm=p, batches=12, ions=600,
                           ionization_fraction=statistics.mean(values),
                           ionization_fraction_sem=statistics.stdev(values) / math.sqrt(12),
                           pressure_times_range_mm_atm=statistics.mean(r['projected_range_mm'] * p for r in group),
                           primary_ionization_kev=statistics.mean(r['primary_ionization_energy_kev'] for r in group),
                           secondary_ionization_kev=statistics.mean(r['secondary_recoil_ionization_energy_kev'] for r in group),
                           backscattered_ions=sum(r['backscattered_ions'] for r in group),
                           transmitted_ions=sum(r['transmitted_ions'] for r in group)))
    return result


def mixture_ratios(points):
    lookup = {campaign.analysis.key(p): p for p in points}
    result = []
    for family in ('Ar', 'Kr', 'Xe'):
        for ion in ('C', 'F', family):
            for energy in (1, 3, 10, 30, 100, 300, 1000):
                low, high = (lookup[family, n, 1., ion, energy] for n in (60, 95))
                row = dict(family=family, ion=ion, energy_kev=energy, pressure_atm=1)
                for field in campaign.analysis.FIELDS:
                    ratio = high[field] / low[field]
                    row[field + '_95_over_60'] = ratio
                    row[field + '_ratio_descriptive_sem'] = ratio * math.hypot(
                        low[field + '_relative_sem'], high[field + '_relative_sem'])
                result.append(row)
    return result


def main():
    run = campaign.RUN
    completion = json.loads((run / 'completion.json').read_text())
    plan, _, jobs = campaign.prepare(run)  # Recheck frozen inputs, never run jobs.
    campaign.CloseoutCampaign(run).audit(run / 'confirmation', jobs, campaign.CONFIG)
    old, new = campaign.previous.read_rows(campaign.previous.RUN), campaign.fresh_rows(run)
    rows = old + new
    if len(new) != 564 or len(rows) != 1841 or sum(r['n_ions'] for r in rows) != 92050:
        raise ValueError('Incomplete closeout dataset')
    if len({r['job_id'] for r in rows}) != len(rows) or len({r['random_seed'] for r in rows}) != len(rows):
        raise ValueError('Duplicate batches/seeds')
    points = campaign.analysis.points(rows)
    fresh = campaign.analysis.points(new)
    tests = campaign.independent_pressure(new, {tuple(g) for g in plan['pressure_groups']})
    raw = []
    for row in new:
        with tarfile.open(row['raw_archive']) as archive:
            texts = [archive.extractfile(name).read().decode('latin1')
                     for name in ('TRIM.IN', 'TDATA.TXT', 'IONIZ.TXT')]
        raw.append(raw_check(row, *texts))
    details = pressure_details(new)
    flags = [p for p in tests if p['flag'] != 'not_flagged']
    ratios = mixture_ratios(points)
    mixture_summary = []
    for family in ('Ar', 'Kr', 'Xe'):
        group = [p for p in ratios if p['family'] == family]
        entry = dict(family=family, comparisons=len(group))
        for field in campaign.analysis.FIELDS:
            values = [p[field + '_95_over_60'] for p in group]
            entry[field + '_ratio_min'] = min(values)
            entry[field + '_ratio_max'] = max(values)
        mixture_summary.append(entry)
    summary = dict(status='PRECISION_COMPLETE_PRESSURE_DIAGNOSTIC_REQUIRED',
                   model_id=campaign.MODEL, points=len(points), histories=92050,
                   points_meeting_targets=sum(p['precision_met'] for p in points),
                   independent_600ion_points=len(fresh),
                   independent_600ion_points_meeting_targets=sum(p['precision_met'] for p in fresh),
                   max_relative_sem={f: max(p[f + '_relative_sem'] for p in points) for f in campaign.analysis.FIELDS},
                   independent_pressure_flags=flags,
                   raw_new_batches_audited=len(raw),
                   density_header_mismatch_batches=sum(r['density_header_mismatch'] for r in raw),
                   max_relative_qf_change_from_endpoint_bin_width=max(abs(r['endpoint_width_qf_relative_change']) for r in raw),
                   retries=sum(r['attempts'] > 1 for r in new),
                   batches_with_warnings=sum(bool(r['warnings']) for r in new),
                   mixture_endpoint_summary=mixture_summary,
                   completion=completion,
                   next_gate='Fixed-input replays and matched-seed pressure/geometry diagnostics; '
                   'diagnostic histories excluded from independent production statistics. '
                   'Resolve reported atomic-density discrepancy before pressure equivalence or broad expansion. '
                   'Do not collect repeated cohorts until a p-value passes.',
                   limitations='Estimated SEM is not physical accuracy or guaranteed post-selection coverage. '
                   'SRIM ionization fraction is not detector-calibrated quenching. '
                   'Non-rejection does not prove pressure equivalence. Surface-injected target geometry '
                   'includes backscattering; do not assume a contained bulk-gas recoil.')
    out = run / 'review'
    atomic_text(out / 'summary.json', json.dumps(summary, indent=2) + '\n')
    for name, data in [('raw_audit', raw), ('kr_f100_pressure', details),
                       ('mixture_endpoint_ratios', ratios), ('pressure_tests', tests)]:
        atomic_text(out / f'{name}.csv', csv_text(data, list(data[0])))
    body = '''<!doctype html><meta charset="utf-8"><title>Reviewed corrected SRIM results</title>
<style>body{font:16px system-ui;max-width:1100px;margin:30px auto;padding:16px}table{border-collapse:collapse}td,th{padding:6px;border:1px solid #ddd}.scroll{overflow:auto}</style>
<h1>Precision complete; pressure interpretation remains gated</h1>
<p>243 direct points, 92,050 histories. All points meet estimated range SEM ≤5% and
ionization-fraction SEM ≤2%; all 47 fresh independent 600-ion cohorts also meet both targets.
Closeout: 1 h 3 min, peak RAM 11.62 GiB, zero scan swap. No new simulations launched by this review.</p>
<p>Coverage: Ar/Kr/Xe, C/F/noble recoils; 60/80/95% noble at 1 atm and
1/3/10/30/100/300/1000 keV (189 points). Additional 0.5/3 atm points only at
80% noble and 1/10/100 keV (54 points). No interpolation or legacy zero-binding results.</p>
<p><a href="../report/index.html">Plots and full dataset</a> · <a href="../report/summary.pdf">PDF</a> ·
<a href="summary.json">Review and next gate</a> · <a href="raw_audit.csv">Raw audit</a> ·
<a href="mixture_endpoint_ratios.csv">95%/60% mixture comparisons</a> ·
<a href="pressure_tests.csv">Independent pressure tests</a></p>
<h2>One independent pressure flag: Kr / F recoil / 100 keV</h2>
<p>At 80% Kr, the ionization-fraction test has BY-adjusted p=0.03355 across twelve
predeclared tests. Means span 3.21% relative (1.71 percentage points). This is a
flag for investigation, not an established physical pressure effect. The four-batch
discovery cohort had the opposite ordering at 0.5 versus 1 atm. Do not merge
discovery and confirmation to replace the predeclared test.</p>'''
    body += table(details, list(details[0]))
    body += '''<h2>Preliminary mixture trends at 1 atm</h2>
<p>Across all 63 endpoint comparisons (three recoils × seven energies × three
families), estimated ranges are longer and ionization fractions lower at 95%
noble than at 60%. The table gives the minimum and maximum 95%/60% ratios over
each family's 21 comparisons, not uncertainty bounds. Individual differences
are not all established statistically; see the CSV for descriptive ratio SEMs.</p>'''
    body += table(mixture_summary, list(mixture_summary[0]))
    body += '''<h2>Input and numerical checks</h2>
<p>All 564 new raw archives/profiles and target binding energies pass validation;
IONIZ reintegration reproduces stored fractions. Input mass density and target gas phase
are correct. At 3 atm, however, TDATA prints 1.351E19 atoms/cm³ instead of approximately
1.352E20 for the 80/20 mixture. This could be text formatting, but its runtime impact
has not been determined. It is not proof that SRIM used the wrong density.</p>
<p>For the flagged group, backscattered ions total 11/26/24 out of 600 at 0.5/1/3 atm,
with no transmitted ions. This is a possible contributor, not a demonstrated cause;
do not remove backscattered histories post hoc. The setup launches ions at a target
surface, so contained bulk-recoil interpretation needs a separate geometry check.</p>
<h2>Next production gate</h2>
<p>Keep broad expansion on hold. Use fixed-input replay followed by a bounded,
matched-seed pressure/geometry diagnostic; preserve the current physical model and
keep diagnostic/replayed histories out of independent counts. Investigate the atomic-density
header discrepancy. Do not sample repeatedly until significance disappears.</p>
<p>Statistical precision does not establish model accuracy. Ionization fraction is not
detector-calibrated quenching. Mixture-ratio SEMs are descriptive delta-method estimates;
the sparse 60/80/95% grid cannot resolve fine concentration features.</p>'''
    atomic_text(out / 'index.html', body)
    print(json.dumps({k: v for k, v in summary.items() if k != 'completion'}, indent=2))


if __name__ == '__main__':
    main()
