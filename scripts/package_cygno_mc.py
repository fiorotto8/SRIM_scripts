#!/usr/bin/env python3
"""Package an already-completed campaign and validated offline CYGNO-MC export.

This command copies results only. It never starts SRIM or modifies MC physics.
The original native archives remain in the source study; a hash inventory and
one complete example per isotope accompany the portable numerical handoff.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from export_cygno_mc import SPECIES, sha, write_csv, write_json


def package(study, export, mc_root, out):
    if out.exists():
        raise ValueError('Use a new output directory; previous handoffs are preserved')
    for source in (study, export):
        if source == out or source in out.parents or out in source.parents:
            raise ValueError('Output must not contain or be contained by a source directory')
    completion = json.loads((study / 'campaign_completion.json').read_text())
    if (study / '.simulation-lock').exists():
        raise ValueError('Simulation lock still exists')
    cfg = json.loads((export / 'snapshot/geant4_config.json').read_text())
    source_matches = {name: sha(mc_root / name) == digest
                      for name, digest in cfg['provenance']['sha256'].items()}
    if not all(source_matches.values()):
        raise ValueError('MC sources differ from supplied configuration; inspect before packaging')
    validation = json.loads((export / 'validation.json').read_text())
    if not validation['passed']:
        raise ValueError('Numerical interchange validation has not passed')
    shutil.copytree(export, out, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'check_response', 'check_response.exe', 'check_response.obj'))
    study_out = out / 'study'
    study_out.mkdir()
    names = ['metadata.json', 'campaign_state.json', 'campaign_completion.json',
             'validation.json', 'archive_validation.json', 'summary.csv', 'comparison.csv',
             'srmodule.csv', 'srmodule_medium_comparison.csv', 'srmodule_medium_comparison.json',
             'systematics.csv', 'systematics.json', 'convergence_diagnostics.json',
             'impurity_diagnostics.json', 'refinement_screening.json', 'events.csv.gz']
    names += [p.name for p in sorted(study.glob('plan_*.json'))]
    for name in names:
        shutil.copy2(study / name, study_out / name)
    for name in ['report', 'provenance', 'research']:
        shutil.copytree(study / name, study_out / name)
    qual = out / 'qualification'
    qual.mkdir(exist_ok=True)
    for name in ['independent_energy_check.py', 'independent_energy_check.json']:
        shutil.copy2(study / 'qualification' / name, qual / name)

    # Canonical wrappers/configs plus historical revision snapshots preserve replay.
    repository = Path(__file__).resolve().parents[1]
    reproduction = out / 'reproduction'
    for name in ['scripts', 'configs']:
        target = reproduction / name
        target.mkdir(parents=True)
        for path in sorted((repository / name).iterdir()):
            if path.is_file() and path.suffix in ('.py', '.sh', '.ps1', '.toml'):
                shutil.copy2(path, target / path.name)
    for name in ['srim_scan.py', 'srim_gas_sweep.py', 'trim_qf_sweep.py',
                 'pysrim_runner.py', 'GasDensities.csv', 'srim-root.txt', 'README.md']:
        shutil.copy2(repository / name, reproduction / name)
    (reproduction / 'docs').mkdir()
    for path in sorted((repository / 'docs').glob('*.md')):
        shutil.copy2(path, reproduction / 'docs' / path.name)
    shutil.copy2(repository / 'scripts/validate_cygno_mc_export.py', out / 'validate_cygno_mc_export.py')
    shutil.copy2(repository / 'docs/cygno_mc_export.md', out / 'TECHNICAL_NOTES.md')

    batches = []
    inventory = []
    examples = out / 'raw_examples'
    examples.mkdir()
    example_species = set()
    for path in sorted((study / 'batches').glob('*.json')):
        batch = json.loads(path.read_text())
        job = batch['job']
        batches.append(job)
        archive = study / batch['raw_archive']
        inventory.append({'job': job, 'raw_archive': str(batch['raw_archive']).replace('\\', '/'),
                          'archive_sha256': sha(archive), 'archive_bytes': archive.stat().st_size,
                          'member_sha256': batch['raw_sha256'], 'source_json_sha256': sha(path)})
        if (job['medium'] == 'baseline' and job['stage'] == 'track_sampling'
                and job['exyz_energy_samples'] == 1000 and job['energy_kevnr'] == 1000
                and job['species'] not in example_species):
            shutil.copy2(archive, examples / (SPECIES[job['species']][0] + '_' + archive.name))
            example_species.add(job['species'])
    assert len(example_species) == 5
    write_json(qual / 'raw_archive_inventory.json', inventory)
    planned = json.loads((study / 'plan_systematics.json').read_text())
    assert {j['job_id'] for j in planned} <= {j['job_id'] for j in batches}
    from collections import Counter
    counts = Counter(j['stage'] for j in batches)
    histories = Counter()
    for job in batches:
        histories[job['stage']] += job['n_ions']

    # Ranges have their original source conditions. A separate density transfer
    # scales every length and its SEM by the same factor; no fake MC histories.
    with (study / 'summary.csv').open() as handle:
        summary = list(csv.DictReader(handle))
    gases = {g['medium']: g for g in json.loads((study / 'metadata.json').read_text())['gas_conditions']}
    fields = ['projected_range_mm_mean', 'projected_range_mm_sem',
              'longitudinal_straggling_mm', 'longitudinal_straggling_mm_sem',
              'lateral_straggling_mm', 'lateral_straggling_mm_sem',
              'endpoint_radius_mm_mean', 'endpoint_radius_mm_sem',
              'primary_sampled_path_mm_mean', 'primary_sampled_path_mm_sem',
              'actual_primary_track_length_mm', 'total_cascade_track_length_mm']
    source_ranges, target_ranges = [], []
    for row in summary:
        energy = float(row['energy_kevnr'])
        if not .25 <= energy <= 1000:
            continue
        species, z, a = SPECIES[row['species']]
        gas = gases[row['medium']]
        result = dict(species=species, Z=z, A=a, medium=row['medium'], energy_keV=energy,
                      QF=float(row['qf_mean']), QF_sem=float(row['qf_sem']),
                      R_keV=float(row['eee_srim_kev_mean']),
                      n_ions=int(row['n_ions_total']), n_batches=int(row['n_batches']),
                      temperature_K=gas['temperature_k'], density_g_cm3=gas['density_g_cm3'],
                      is_direct=1, method='direct production/refinement; nominal21eV',
                      energy_budget_valid=row['energy_budget_valid'])
        result.update({key: float(row[key]) if row[key] else None for key in fields})
        source_ranges.append(result)
        if row['medium'] == 'baseline':
            scale = gas['density_g_cm3'] / cfg['target']['density_g_cm3']
            target = {**result, 'temperature_K': cfg['target']['temperature_K'],
                      'density_g_cm3': cfg['target']['density_g_cm3'], 'is_direct': 0,
                      'method': 'derived density transfer; lengths and SEMs multiplied by rho_source/rho_target; model error unassigned'}
            target.update({key: result[key] * scale if result[key] is not None else None for key in fields})
            target_ranges.append(target)
    write_csv(out / 'ranges_source_direct.csv', source_ranges)
    write_csv(out / 'ranges_target_transferred.csv', target_ranges)

    (out / 'example_response.cc').write_text('''#include "cygno_srim_response.hpp"
#include <iostream>
int main() {
    const double initial_keV = 10.0;
    auto r = cygno_srim::evaluate(8, 16, initial_keV);
    std::cout << "O16 mean electronic energy [keV] = " << initial_keV*r.QF
              << "; QF = " << r.QF << "; QF SEM = " << r.QF_sem
              << "; d(E*QF)/dE = " << r.dRdE << '\\n';
}
''', encoding='utf-8', newline='\n')
    handoff = {'schema': 'CYGNO-MC-completed-SRIM-handoff-v1',
               'packaged_at': datetime.now(timezone.utc).isoformat(),
               'source_study': str(study), 'raw_archives_location': str(study / 'raw'),
               'raw_archives_included': 'Five complete baseline examples only; all archives inventoried with SHA256',
               'source_campaign_completion': completion['status'],
               'report_status': 'Regenerated from completed campaign; original validation flags retained',
               'batch_counts': dict(counts), 'history_counts': dict(histories),
               'total_batches': len(batches), 'total_histories_including_controls': sum(histories.values()),
               'final_planned_systematic_batches': len(planned),
               'mc_HEAD_verified': subprocess.check_output(['git', '-C', str(mc_root), 'rev-parse', 'HEAD'], text=True).strip(),
               'mc_config_original_HEAD': cfg['provenance']['HEAD'], 'mc_source_hash_matches': source_matches,
               'mc_physics_modified': False, 'interchange_numerical_validation': validation,
               'range_source_note': 'Production/refinement aggregates; QF fits additionally include independent nominal coarse track-sampling controls. Endpoints remain source293K.',
               'range_density_transfer_scale': gases['baseline']['density_g_cm3']/cfg['target']['density_g_cm3'],
               'limitations': ['Physical charge calibration unavailable', 'Exact-target293.15K direct coverage missing',
                               'Derivative not validated as an independent per-hit multiplier',
                               'Below0.25keV terminal policy unspecified', 'Full3D primary/cascade arc lengths unavailable',
                               'Displacement threshold and track-sampling sensitivities excluded from MC SEM',
                               'All files under MC tests/ are Git-ignored; explicit copy required for transfer']}
    handoff['range_coverage'] = []
    for medium, species in sorted({(r['medium'], r['species']) for r in source_ranges}):
        energies = [r['energy_keV'] for r in source_ranges
                    if (r['medium'], r['species']) == (medium, species)]
        handoff['range_coverage'].append(dict(
            medium=medium, species=species, direct_points=len(energies),
            source_min_keV=min(energies), source_max_keV=max(energies),
            direct_at_0_25_keV=.25 in energies, direct_at_1000_keV=1000 in energies,
            range_interpolation_supplied=False))
    write_json(out / 'HANDOFF.json', handoff)
    return handoff


def seal(out):
    paths = sorted(p for p in out.rglob('*') if p.is_file() and p != out / 'SHA256SUMS'
                   and '__pycache__' not in p.parts and p.suffix != '.pyc')
    (out / 'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(out).as_posix()}\n' for p in paths), encoding='utf-8', newline='\n')
    archive_path = out.with_name(out.name + '.zip')
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths + [out / 'SHA256SUMS']:
            archive.write(path, (Path(out.name) / path.relative_to(out)).as_posix())
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
    archive_path.with_suffix('.zip.sha256').write_text(f'{sha(archive_path)}  {archive_path.name}\n', encoding='utf-8', newline='\n')
    return archive_path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['study', 'export', 'mc-root', 'out']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    result = package(args.study.resolve(), args.export.resolve(), args.mc_root.resolve(), args.out.resolve())
    print(json.dumps(result, indent=2))
    # Add/review the handoff README and summary, then seal() explicitly.
