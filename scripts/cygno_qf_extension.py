#!/usr/bin/env python3
"""Bounded CYGNO extension: only missing 25/40/63 eV baseline anchors.

Native simulation code uses the existing coordinator and no scientific Python
packages. Postprocessing uses the same native Python with NumPy/SciPy/Matplotlib.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]
import cygno_study as study
import srim_scan as core

PUBLICATION = ROOT / 'studies/cygno_qf_900mbar'
PARENT = ROOT / 'runs/cygno_hecf4_900mbar_20261015'
EXTENSION = PARENT / 'extensions/low_energy_20261017'
ENERGIES = (.025, .040, .063)


def inventory(parent=PARENT, publication=PUBLICATION):
    """Use the qualified complete snapshot, rejecting any unsnapshotted batches."""
    snapshot = publication / 'frozen/snapshot'
    for name, expected in json.loads((snapshot / 'sha256.json').read_text()).items():
        if core.sha256_file(snapshot / name) != expected:
            raise ValueError('Frozen source changed: ' + name)
    captured = json.loads((snapshot / 'capture.json').read_text())
    paths = sorted((parent / 'batches').glob('*.json'))
    if len(paths) != captured.get('parent_listed_source_batch_count',captured['listed_source_batch_count']):
        raise ValueError('Parent campaign changed: refresh its complete nominal snapshot before extending')
    if (parent / 'metadata.json').read_bytes() != (snapshot / 'source_metadata.json').read_bytes():
        raise ValueError('Parent gas/configuration differs from the completed snapshot')
    records = [json.loads(line) for line in (snapshot / 'batches.jsonl').read_text().splitlines()]
    grouped = defaultdict(list)
    for record in records:
        job = record['job']
        origin=parent
        if job['stage']=='low_energy_extension':
            origin=parent/'extensions'/record['source_study'].replace('\\','/').split('/')[-1]
        if not (origin / record['source_json']).is_file() or not (origin / record['raw_archive']).is_file():
            raise ValueError('Missing original result/archive: ' + job['job_id'])
        grouped[(job['species'], job['energy_kevnr'])].append(record)
    rows = []
    for (symbol, energy), batches in sorted(grouped.items()):
        stages = Counter(b['job']['stage'] for b in batches)
        rows.append(dict(species=symbol, energy_keV=energy, n_batches=len(batches),
                         n_ions=sum(b['job']['n_ions'] for b in batches),
                         stages=dict(stages), batch_ids=[b['job']['job_id'] for b in batches],
                         independent_seeds=[b['job']['seed'] for b in batches],
                         source_temperature_K=293., source_density_g_cm3=0.0013891949590458925,
                         role='compatible nominal coarse control' if set(stages)=={'track_sampling'}
                         else 'new nominal extension' if set(stages)=={'low_energy_extension'} else 'nominal production/refinement'))
    return dict(parent_batch_records=len(paths), compatible_nominal_batches=len(records),
                compatible_nominal_histories=sum(b['job']['n_ions'] for b in records),
                points=rows, snapshot_sha256=core.sha256_file(snapshot / 'sha256.json'),
                excluded_datasets=captured['exclusions'],
                controls='Nominal coarse track_sampling controls remain separately tagged; nonnominal threshold, fine correlated sampling, pilots and input controls never enter the fit')


def extension_jobs(existing, cfg):
    covered = {(r['species'], r['energy_keV']) for r in existing['points']}
    jobs = []
    for symbol in cfg['study']['species']:
        for energy in ENERGIES:
            if (symbol, energy) in covered:
                continue
            for repeat in range(3):
                jobs.append(dict(stage='low_energy_extension', medium='baseline', species=symbol,
                                 energy_kevnr=energy, repeat=repeat, n_ions=100,
                                 displacement_energy_ev=21., exyz_energy_samples=1000))
    result = study.finalize_jobs(jobs, cfg)
    assert len(result) <= 45 and len({j['seed'] for j in result}) == len(result)
    return result


def run_extension(source, out, cfg, config_path, workers):
    if workers < 1:
        raise ValueError('workers must be positive')
    if out.parent.name != 'extensions' or not (out.parent.parent / 'metadata.json').is_file():
        raise ValueError('Use the existing campaign extensions/ directory; never the parent campaign itself')
    parent = out.parent.parent
    state = dict(pid=os.getpid(), host=study.socket.gethostname(), coordinator_platform=os.name,
                 status='running', started_at=core.utc_now(), mode='bounded_low_energy_extension')
    with study.study_lock(out):
        core.atomic_write_json(out / 'campaign_state.json', state)
        try:
            if (out / '.stop-requested').exists():
                raise RuntimeError('Stop marker present; use the existing control script to resume')
            previous_plan = out / 'extension_plan.json'
            existing = (json.loads(previous_plan.read_text())['original_inventory']
                        if previous_plan.exists() else inventory(parent))
            parent_cfg = json.loads((parent / 'metadata.json').read_text())['configuration']
            if cfg != parent_cfg:
                raise ValueError('Reuse the exact established configuration')
            metadata, elements = study.prepare(out, cfg, config_path, source)
            jobs = extension_jobs(existing, cfg)
            plan = dict(created_at=core.utc_now(), parent_study=str(parent),
                        requested_energies_keV=list(ENERGIES), jobs=jobs,
                        maximum_new_batches=45, histories_per_batch=100,
                        batches_per_new_point=3, adaptive_expansion=False,
                        runner_sha256=core.sha256_file(Path(__file__)), original_inventory=existing)
            if previous_plan.exists():
                if json.loads(previous_plan.read_text())['jobs'] != jobs:
                    raise ValueError('Refusing extension job/seed drift on resume')
            else:
                core.atomic_write_json(previous_plan, plan)
            first_runner=out/'provenance/cygno_qf_extension.py'
            if not first_runner.exists():shutil.copy2(Path(__file__),first_runner)
            versioned_runner=out/'provenance/source_revisions'/('extension-'+core.sha256_file(Path(__file__))+'.py')
            if not versioned_runner.exists():shutil.copy2(Path(__file__),versioned_runner)
            if not jobs:
                state.update(status='complete_with_low_energy_model_limitations',completed_batches=0,
                             expected_batches=0,reason='All requested points already exist; no new histories launched')
                core.atomic_write_json(out/'campaign_completion.json',state)
                return 0
            if not (out / 'srmodule.csv').exists():
                shutil.copy2(parent / 'srmodule.csv', out / 'srmodule.csv')
            shutil.copytree(parent / 'raw/srmodule', out / 'raw/srmodule', dirs_exist_ok=True)
            success = study.execute(source, out, cfg, metadata, elements, 'low_energy_extension',
                                    workers, jobs_override=jobs, plan_name='low_energy_extension')
            completed = {p.stem for p in (out / 'batches').glob('*.json')}
            missing = [j['job_id'] for j in jobs if j['job_id'] not in completed]
            state.update(completed_batches=len(completed), expected_batches=len(jobs), missing_jobs=missing)
            state['status'] = ('simulations_complete_report_pending' if success and not missing
                               else 'stopped' if (out / '.stop-requested').exists() else 'failed')
            core.atomic_write_json(out / 'campaign_completion.json', state)
            return 0 if success and not missing else 1
        except BaseException as error:
            state.update(status='failed', error_type=type(error).__name__, error=str(error))
            raise
        finally:
            state['finished_at'] = core.utc_now()
            core.atomic_write_json(out / 'campaign_state.json', state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inventory', 'finish', 'audit'])
    parser.add_argument('--run-dir', type=Path, default=EXTENSION)
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--output',type=Path,help='New inventory JSON file; omitted means read-only summary')
    args = parser.parse_args()
    if args.command == 'inventory':
        record = inventory()
        if args.output:
            if args.output.exists():parser.error('--output must be a new file')
            args.output.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8',newline='\n')
        print(json.dumps({k:v for k,v in record.items() if k != 'points'}, indent=2))
    elif args.command=='audit':
        from cygno_qf_extension_analysis import check_new_archives
        result=check_new_archives(args.run_dir.resolve(),PUBLICATION)
        print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
    else:
        finish(args.run_dir.resolve(), wait=args.wait)


def finish(out, wait=False):
    import time
    while True:
        state = json.loads((out/'campaign_state.json').read_text())
        if state['status'] != 'running':
            break
        if not wait:
            raise RuntimeError('Simulation still running; use finish --wait for detached postprocessing')
        print('Waiting for bounded native extension; no additional simulations are launched', flush=True)
        time.sleep(20)
    if state['status']=='complete_with_low_energy_model_limitations':
        print('Extension and publication already complete; no new simulations or fits launched')
        return
    if state['status'] != 'simulations_complete_report_pending':
        raise RuntimeError('Extension is incomplete: ' + state['status'])
    from cygno_qf_extension_analysis import publish
    publish(out, PUBLICATION)


if __name__ == '__main__':
    main()
