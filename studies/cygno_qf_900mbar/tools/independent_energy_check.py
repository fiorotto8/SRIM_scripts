#!/usr/bin/env python3
"""Read-only Decimal crosscheck of archived SRIM energy histograms.

Independent of the production parsers: use the first depth label as the bin
width (native labels are positive integer multiples of that width), Decimal
arithmetic, and no additional division by incident-ion count. This is a
numerical reproduction check, not validation of the SRIM gas/charge model.
"""
import argparse
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re
import tarfile


def profile(data, requested_ions, electronic, header_count_tolerance=Decimal('0.011')):
    text = data.decode('latin1')
    count = re.search(r'Total\s+Ions\s+calculated\s*=\s*([\d.Ee+-]+)', text)
    assert count, 'missing incident-ion count'
    printed_count = Decimal(count[1])
    # Native low-energy headers can say 99.99 for 100 actual endpoints (and
    # 4.99 for five). Preserve this anomaly; actual counts are checked below.
    assert abs(printed_count - requested_ions) <= header_count_tolerance, 'incident-ion count mismatch'
    units = re.search(r'Units\s+are\s*>+\s*(.*?)\s*<+', text, re.I)
    assert units and 'angstrom-ion' in units[1].replace(' ', '').lower(), 'missing per-ion/depth units'
    assert ('ev' if electronic else 'phonons') in units[1].lower(), 'unexpected channel units'
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        try:
            row = tuple(Decimal(value.replace('D', 'E')) for value in fields)
        except InvalidOperation:
            continue
        if row[0] > 0:
            rows.append(row)
    assert len(rows) == 100, 'unexpected histogram size'
    width = rows[0][0]
    assert all(abs(row[0] / width - index) < Decimal('0.001')
               for index, row in enumerate(rows, 1)), 'nonuniform native depth labels'
    return [(sum(row[column] for row in rows) * width / 1000)
            for column in (1, 2)], printed_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study-dir', type=Path, required=True,
                        help='Original campaign directory with batches/ and raw/; not bundled')
    parser.add_argument('--out', type=Path, help='New JSON output file; otherwise print summary only')
    args = parser.parse_args()
    root = args.study_dir.resolve()
    if args.out and args.out.exists():
        parser.error('--out must be a new file')
    if not list((root / 'batches').glob('*.json')):
        parser.error('--study-dir contains no archived batch records')
    results = []
    for path in sorted((root / 'batches').glob('*.json')):
        batch = json.loads(path.read_text())
        job = batch['job']
        with tarfile.open(root / batch['raw_archive']) as archive:
            members = {Path(member.name).name.upper(): member for member in archive.getmembers()}
            ioniz = archive.extractfile(members['IONIZ.TXT']).read()
            phonon = archive.extractfile(members['PHONON.TXT']).read()
        assert batch['effective_settings']['n_ions_calculated'] == job['n_ions']
        assert batch['endpoints']['n_records'] == job['n_ions']
        (primary, secondary), ioniz_count = profile(ioniz, job['n_ions'], True)
        (primary_phonon, secondary_phonon), phonon_count = profile(phonon, job['n_ions'], False)
        electronic = primary + secondary
        phonon_total = primary_phonon + secondary_phonon
        stored_e = Decimal(str(batch['budget']['total_electronic_energy_kev']))
        stored_p = Decimal(str(batch['budget']['phonon_energy_kev']))
        rel_e = abs(electronic - stored_e) / max(abs(stored_e), Decimal('1e-20'))
        rel_p = abs(phonon_total - stored_p) / max(abs(stored_p), Decimal('1e-20'))
        initial = Decimal(str(job['energy_kevnr']))
        binding = Decimal(str(batch['budget']['binding_energy_sink_kev']))
        results.append({'job_id': job['job_id'], 'stage': job['stage'], 'medium': job['medium'],
            'species': job['species'], 'initial_energy_kevnr': float(initial), 'n_ions': job['n_ions'],
            'ioniz_printed_count': float(ioniz_count), 'phonon_printed_count': float(phonon_count),
            'primary_electronic_kev': float(primary), 'secondary_electronic_kev': float(secondary),
            'secondary_fraction_of_electronic': float(secondary / electronic),
            'electronic_kev': float(electronic), 'phonon_kev': float(phonon_total),
            'relative_difference_electronic': float(rel_e), 'relative_difference_phonon': float(rel_p),
            'energy_closure_residual_fraction': float((initial - electronic - phonon_total - binding) / initial),
            'passed': max(rel_e, rel_p) <= Decimal('0.0003'),
            'raw_archive': batch['raw_archive']})
    failures = [row['job_id'] for row in results if not row['passed']]
    record = {'status': 'passed' if results and not failures else 'failed', 'records': results,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Snapshot of completed batch files at invocation; rerun after the final campaign to cover final outputs.',
        'checked_batches': len(results), 'failed_jobs': failures,
        'maximum_relative_difference_electronic': max(row['relative_difference_electronic'] for row in results),
        'maximum_relative_difference_phonon': max(row['relative_difference_phonon'] for row in results),
        'comparison_relative_tolerance': 0.0003,
        'method': 'Decimal sum of every native bin times first positive depth label; native per-incident-ion normalization retained. Production uses median depth differences. Tiny discrepancies from printed depth precision are retained.',
        'limitations': 'Numerical reproduction only. PHONON uses the documented SRIM energy convention; binding bookkeeping uses the archived effective-settings result. This does not establish physical charge QF or resolve transport cutoffs.',
        'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({key: value for key, value in record.items() if key != 'records'}, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
