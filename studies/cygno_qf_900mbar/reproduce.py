#!/usr/bin/env python3
"""Verify or replay the published CYGNO QF dataset; never runs SRIM or Geant4."""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
DATASET = Path(__file__).resolve().parent
SPECIES = ['H1', 'He4', 'C12', 'O16', 'F19']


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_exporter(frozen):
    spec = importlib.util.spec_from_file_location('published_exporter', frozen / 'export_cygno_mc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_tables(frozen, destination):
    import numpy as np
    exporter = load_exporter(frozen)
    destination.mkdir(parents=True, exist_ok=True)
    with (frozen / 'response_target_smooth.csv').open() as handle:
        response = list(csv.DictReader(handle))
    models = {m['species']: m for m in json.loads((frozen / 'response_models.json').read_text())['models']}
    manifest=json.loads((frozen/'manifest.json').read_text())
    for species in SPECIES:
        rows = [row.copy() for row in response if row['species'] == species]
        energy = np.array([float(row['energy_keV']) for row in rows])
        model = models[species]
        slope_basis = exporter.basis(model['knots_log_energy'], np.log(energy), 1) / energy[:, None]
        slope = slope_basis @ model['coefficients']
        sem = np.sqrt(np.maximum(0., np.einsum('...i,ij,...j->...', slope_basis,
                                             model['coefficient_covariance'], slope_basis)))
        for i, row in enumerate(rows):
            row['dQF_dE_per_keV'] = float(slope[i])
            row['dQF_dE_sem_per_keV'] = float(sem[i])
            row['valid_energy_min_keV'],row['valid_energy_max_keV']=model['valid_energy_keV']
            row['source_temperature_K']=manifest['source_gas']['temperature_k']
            row['target_temperature_K']=manifest['target']['temperature_K']
            row['physical_charge_calibrated']=0
            row['low_energy_extension_closure_warning']=int(float(row['energy_keV'])<.1)
        exporter.write_csv(destination / (species + '.csv'), rows)
    shutil.copy2(frozen / 'qf_source_with_guards.csv', destination / 'raw_points.csv')
    shutil.copy2(frozen / 'energy_channels_source.csv', destination / 'energy_channels_source.csv')
    controls = json.loads((DATASET / 'validation/systematics.json').read_text())['rows']
    exporter.write_csv(destination / 'threshold_sensitivity.csv',
                       [row for row in controls if row.get('comparison') == 'threshold_vs_nominal'])
    shutil.copy2(DATASET / 'validation/comparison.csv', destination / 'impurity_comparison.csv')
    reference = json.loads((DATASET / 'provenance/thesis_crosscheck.json').read_text())
    parameters = reference['thesis']['parameters_k_a_b']
    comparison = []
    for row in response:
        species, energy = row['species'], float(row['energy_keV'])
        if species in parameters:
            k, a, b = parameters[species]
            f = k * (energy + a * energy**b)
            old_q = f / (1 + f)
            old_d = old_q + energy*k*(1 + a*b*energy**(b-1))/(1+f)**2
        else:
            old_q = old_d = ''
        current_q, current_d = (old_q, old_d) if species in {'He4', 'C12', 'F19'} else (1., 1.)
        comparison.append(dict(species=species, energy_keV=energy,
                               QF_new=row['QF'], QF_new_sem=row['QF_sem'],
                               dR_dE_new=row['dR_dE'], dR_dE_new_sem=row['dR_dE_sem'],
                               QF_thesis=old_q, dR_dE_thesis=old_d,
                               thesis_fit_available=int(species in parameters),
                               within_thesis_fit_interval=int(species in parameters and 1 <= energy <= 500),
                               QF_MC_reference=current_q, dR_dE_MC_reference=current_d,
                               MC_reference_date='2026-10-16',
                               MC_reference_behavior='analytic_fit' if species in {'He4', 'C12', 'F19'}
                               else 'proton_excluded_NR_raw_deposits' if species == 'H1' else 'oxygen_unity_fallback',
                               new_curve_is_direct=0))
    exporter.write_csv(destination / 'thesis_mc_comparison.csv', comparison)
    if (DATASET/'validation/derivative_stability.json').exists():
        make_extension_tables(frozen,destination)


def make_extension_tables(frozen,destination):
    import numpy as np
    exporter=load_exporter(frozen)
    models=json.loads((frozen/'response_models.json').read_text())['models']
    previous={m['species']:m for m in json.loads((DATASET/'provenance/previous_response_models.json').read_text())['models']}
    diagnostics=json.loads((DATASET/'validation/derivative_stability.json').read_text())
    comparison=[];stability=[]
    for model in models:
        species=model['species'];energy=np.geomspace(*model['valid_energy_keV'],801)
        q,_,d,_=exporter.evaluate(model,energy)
        alternatives=[exporter.evaluate(m,energy) for m in diagnostics['sensitivity_models'][species]]
        allq=np.array([q]+[a[0] for a in alternatives]);alld=np.array([d]+[a[2] for a in alternatives])
        for i,e in enumerate(energy):
            stability.append(dict(species=species,energy_keV=float(e),QF=float(q[i]),dR_dE=float(d[i]),
                                  QF_model_sensitivity_min=float(allq[:,i].min()),QF_model_sensitivity_max=float(allq[:,i].max()),
                                  dR_dE_model_sensitivity_min=float(alld[:,i].min()),dR_dE_model_sensitivity_max=float(alld[:,i].max())))
        overlap=np.geomspace(.25,1000,501)
        new=exporter.evaluate(model,overlap);old=exporter.evaluate(previous[species],overlap)
        no_low=exporter.evaluate(diagnostics['no_new_low_energy_models'][species],overlap)
        for i,e in enumerate(overlap):
            comparison.append(dict(species=species,energy_keV=float(e),QF_previous=float(old[0][i]),
                                   QF_updated=float(new[0][i]),QF_change=float(new[0][i]-old[0][i]),
                                   dR_dE_previous=float(old[2][i]),dR_dE_updated=float(new[2][i]),
                                   dR_dE_change=float(new[2][i]-old[2][i]),
                                   QF_without_new_low_points=float(no_low[0][i]),
                                   dR_dE_without_new_low_points=float(no_low[2][i])))
    exporter.write_csv(destination/'previous_response_comparison.csv',comparison)
    exporter.write_csv(destination/'derivative_stability.csv',stability)
    with (frozen/'energy_channels_source.csv').open() as handle:
        new_points=[r for r in csv.DictReader(handle) if float(r['energy_keV']) in (.025,.04,.063)]
    exporter.write_csv(destination/'extension_points.csv',new_points)
    inventory=[]
    with (DATASET/'validation/summary.csv').open() as handle:
        for r in csv.DictReader(handle):
            inventory.append(dict(medium=r['medium'],species=r['species'],energy_keV=float(r['energy_kevnr']),
                                  n_batches=int(r['n_batches']),n_ions=int(r['n_ions_total']),
                                  dataset_role='nominal_production_refinement',
                                  compatible_with_baseline=int(r['medium']=='baseline')))
    original=json.loads((DATASET/'validation/extension_inventory.json').read_text())
    for r in original['points']:
        if r['role']=='compatible nominal coarse control':
            inventory.append(dict(medium='baseline',species=r['species'],energy_keV=r['energy_keV'],
                                  n_batches=r['n_batches'],n_ions=r['n_ions'],
                                  dataset_role='compatible_nominal_coarse_control',compatible_with_baseline=1))
    isotope_to_symbol={'H1':'H','He4':'He','C12':'C','O16':'O','F19':'F'}
    for r in new_points:
        inventory.append(dict(medium='baseline',species=isotope_to_symbol[r['species']],energy_keV=float(r['energy_keV']),
                              n_batches=int(r['n_batches']),n_ions=int(r['n_ions']),
                              dataset_role='new_nominal_extension',compatible_with_baseline=1))
    exporter.write_csv(destination/'point_inventory.csv',inventory)


def verify():
    import numpy as np
    expected = {}
    for line in (DATASET / 'SHA256SUMS').read_text().splitlines():
        value, name = line.split('  ', 1)
        path = (DATASET / name).resolve()
        if DATASET not in path.parents:
            raise ValueError('Invalid checksum path')
        if digest(path) != value:
            raise ValueError('Published file changed: ' + name)
        expected[name] = value
    actual = {p.relative_to(DATASET).as_posix() for p in DATASET.rglob('*')
              if p.is_file() and p.name != 'SHA256SUMS' and '__pycache__' not in p.parts
              and p.suffix != '.pyc'}
    if actual != set(expected):
        raise ValueError('Published file inventory differs from SHA256SUMS')
    exporter = load_exporter(DATASET / 'frozen')
    _, _, records = exporter.load_snapshot(DATASET / 'frozen/snapshot')
    models={m['species']:m for m in json.loads((DATASET/'frozen/response_models.json').read_text())['models']}
    for species in SPECIES:
        with (DATASET / 'tables' / (species + '.csv')).open() as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 401 and all(row['species'] == species for row in rows)
        e, q, d, slope = np.array([[float(row[key]) for key in
                                  ['energy_keV', 'QF', 'dR_dE', 'dQF_dE_per_keV']] for row in rows]).T
        assert [e[0],e[-1]] == models[species]['valid_energy_keV'] and np.all(np.diff(e) > 0)
        assert np.all((q >= 0) & (q <= 1 + 1e-12)) and np.all(np.diff(e*q) > 0)
        np.testing.assert_allclose(d, q + e*slope, rtol=1e-12, atol=1e-14)
        assert all(row['is_direct'] == '0' and row['n_ions'] == '0' for row in rows)
    with tempfile.TemporaryDirectory(prefix='cygno-tables-') as temp:
        destination = Path(temp) / 'tables'
        make_tables(DATASET / 'frozen', destination)
        for path in destination.iterdir():
            assert digest(path) == digest(DATASET / 'tables' / path.name), path.name
    print(json.dumps({'published_hashes_verified': len(expected), 'source_batches': len(records),
                      'species': SPECIES, 'rows_per_species': 401, 'table_derivatives_verified': True}, indent=2))


def replay(out):
    out = out.resolve()
    if out.exists() or DATASET == out or DATASET in out.parents or out in DATASET.parents:
        raise ValueError('Use a new replay directory outside the publication')
    out.mkdir(parents=True)
    environment = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
    frozen = DATASET / 'frozen'
    commands = [
        [sys.executable, str(frozen / 'export_cygno_mc.py'), '--snapshot', str(frozen / 'snapshot'), '--out', str(out / 'frozen')],
        [sys.executable, str(frozen / 'validate_cygno_mc_export.py'), str(out / 'frozen'), '--compare', str(frozen)],
        [sys.executable, str(DATASET / 'tools/make_recap.py'), '--export-dir', str(out / 'frozen'), '--out', str(out / 'plots')],
    ]
    for index, command in enumerate(commands, 1):
        print(f'Replay step {index}/{len(commands)}', flush=True)
        with (out / f'step_{index}.log').open('w') as handle:
            subprocess.run(command, env=environment, stdout=handle, stderr=subprocess.STDOUT, check=True)
    make_tables(out / 'frozen', out / 'tables')
    for path in (out / 'tables').iterdir():
        assert digest(path) == digest(DATASET / 'tables' / path.name), path.name
    print('Replayed numerical CSVs/models/header and every published table match byte-for-byte:', out)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--out', type=Path)
    args = parser.parse_args()
    verify()
    if args.out:
        replay(args.out)
