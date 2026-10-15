#!/usr/bin/env python3
"""Reproducible native-SRIM bulk He/CF4 study; reuse existing SRIM adapters.

No source installation, legacy inputs, archived results or branch is modified.
Outputs are direct full-cascade calculations; SRModule interpolation remains a
separately labelled stopping/range prediction, never a substitute for TRIM.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import srim_scan as core
import srim_gas_sweep as stopping
import trim_qf_sweep as trim
import cygno_study_physics as physics

ISOTOPE_MASSES = {'H':1.00782503223,'He':4.00260325413,'C':12.0,'F':18.99840316273,'O':15.99491461957}
FORMULAS = {'He':{'He':1}, 'CF4':{'C':1,'F':4}, 'H2O':{'H':2,'O':1}, 'O2':{'O':2}}

def log_grid(lo, hi, n):
    return [float(f'{lo*(hi/lo)**(i/(n-1)):.8g}') for i in range(n)]

def load_configuration(path):
    cfg=tomllib.loads(path.read_text())
    s=cfg['study']; sim=cfg['simulation']; grid=cfg['grid']
    assert math.isclose(s['he_mole_fraction_dry']+s['cf4_mole_fraction_dry'],1.)
    assert sim['cascade_type']==5 and sim['independent_batches']>=3
    assert sim['workers']>=1 and sim['ions_per_batch']>0 and 3<=sim['independent_batches']<=sim['maximum_batches']<=9
    assert 200000<=sim['maximum_secondary_recoils']<=10000000
    assert grid['energy_min_kevnr']>0 and grid['energy_max_kevnr']>grid['energy_min_kevnr']
    return cfg

def gas_conditions(cfg, elements):
    s=cfg['study']; impurities=(s['h2o_ppm_molar']+s['o2_ppm_molar'])*1e-6
    wet={'He':s['he_mole_fraction_dry']*(1-impurities),'CF4':s['cf4_mole_fraction_dry']*(1-impurities),
         'H2O':s['h2o_ppm_molar']*1e-6,'O2':s['o2_ppm_molar']*1e-6}
    conditions=[]
    for name,molecular in [('baseline',{'He':s['he_mole_fraction_dry'],'CF4':s['cf4_mole_fraction_dry']}),('contaminated',wet)]:
        stoich={}
        for molecule,fraction in molecular.items():
            for symbol,count in FORMULAS[molecule].items():
                stoich[symbol]=stoich.get(symbol,0.)+fraction*count
        mw=sum(elements[k].mass_u*v for k,v in stoich.items())
        density=s['pressure_mbar']*100*mw/(8.31446261815324*s['temperature_k'])*1e-6
        conditions.append({'medium':name,'molecular_fractions':molecular,'element_counts_per_molecule':stoich,
            'atomic_fractions':{k:v/sum(stoich.values()) for k,v in stoich.items()},
            'mass_fractions':{k:v*elements[k].mass_u/mw for k,v in stoich.items()},
            'molecular_weight_g_mol':mw,'density_g_cm3':density,'pressure_mbar':s['pressure_mbar'],
            'temperature_k':s['temperature_k'],'target_mass_source':'Installed Data/ATOMDATA natural elemental masses'})
    return conditions

def as_gas(condition):
    return stopping.Gas(1,condition['medium'],condition['density_g_cm3'],condition['pressure_mbar']/1013.25,
        condition['element_counts_per_molecule'],list(condition['molecular_fractions']),temperature=condition['temperature_k'])

def prepare(out,cfg,config_path,source):
    out.mkdir(parents=True,exist_ok=True)
    elements,_=stopping.load_elements(source/'Data/ATOMDATA')
    metadata={'expected_summary_rows':2*len(cfg['study']['species'])*cfg['grid']['energy_points'],'schema_version':1,'configuration':cfg,'gas_conditions':gas_conditions(cfg,elements),
        'isotope_projectile_masses_u':ISOTOPE_MASSES,'isotope_mass_source':'https://pml.nist.gov/cgi-bin/Compositions/stand_alone.pl?ascii=ascii',
        'source_root':str(source),'git_commit':core.git_head(ROOT),'execution_backend':'native Windows executables',
        'qf_definition':'(IONIZ primary + IONIZ secondary electronic energy) / initial nuclear recoil energy',
        'eee_definition':'SRIM electronic-energy surrogate only; physical charge/light keVee requires separate recoil calibration',
        'actual_total_3d_trajectory_length':None,'created_at':core.utc_now()}
    path=out/'metadata.json'
    if path.exists():
        existing=json.loads(path.read_text())
        if existing['configuration']!=cfg or existing['gas_conditions']!=metadata['gas_conditions']:
            raise RuntimeError('Refusing configuration drift in existing study directory')
    else:
        core.atomic_write_json(path,metadata)
    provenance=out/'provenance'; provenance.mkdir(exist_ok=True)
    sources=[config_path,Path(__file__),ROOT/'scripts/cygno_study_physics.py',ROOT/'scripts/cygno_native_run.py',ROOT/'scripts/cygno_study_adaptive.py',ROOT/'scripts/cygno_study_validation.py',ROOT/'scripts/start_cygno_study.sh',ROOT/'scripts/cygno_study_report.py',ROOT/'srim_scan.py',ROOT/'srim_gas_sweep.py',ROOT/'trim_qf_sweep.py',ROOT/'docs/cygno_srim_methodology.md']
    manifest={path.name:core.sha256_file(path) for path in sources}
    revision=hashlib.sha256(core.canonical_json(manifest).encode()).hexdigest()
    snapshot=provenance/'source_revisions'/revision
    snapshot.mkdir(parents=True,exist_ok=True)
    for path in sources:
        frozen=snapshot/path.name
        if frozen.exists() and core.sha256_file(frozen)!=manifest[path.name]:
            raise RuntimeError('Source snapshot integrity failure: '+str(frozen))
        if not frozen.exists():shutil.copy2(path,frozen)
        if core.sha256_file(frozen)!=manifest[path.name]:raise RuntimeError('Source changed during snapshot: '+str(path))
        shutil.copy2(path,provenance/path.name)
    core.atomic_write_json(snapshot/'manifest.json',manifest)
    core.atomic_write_json(provenance/'source_manifest.json',manifest)
    core.atomic_write_json(provenance/'current_source_revision.json',{'revision':revision,'manifest':manifest})
    metadata['execution_source_revision']=revision
    installation=provenance/'source_installation.json'
    if not installation.exists():
        core.atomic_write_json(installation,{'fingerprint':core.source_fingerprint(source),'control':core.source_control_fingerprint(source)})
    return metadata,elements

def normalize_generated_crlf(path):
    # Existing writers explicitly emit CRLF; Windows text mode can double CR.
    data=path.read_bytes().replace(b'\r\r\n',b'\r\n')
    path.write_bytes(data)

def runtime_for(source,out,name,capacity=2000000):
    runtime=out/'runtime'/name
    core.prepare_runtime(source,runtime,core.source_fingerprint(source))
    path=runtime/'Data/TRIM.cfg'
    data=path.read_bytes()
    data,count=re.subn(rb'(Maximum number of secondary recoils[^\r\n]*\r?\n)[ \t]*[0-9]+[ \t]*',lambda m:m[1]+f' {int(capacity)} '.encode('ascii'),data)
    if count!=1: raise RuntimeError('Cannot identify recoil capacity')
    path.write_bytes(data)
    return runtime

def run_srmodule(source,out,cfg,metadata,elements):
    runtime=runtime_for(source,out,'stopping',cfg['simulation']['maximum_secondary_recoils'])
    rows=[]
    for condition in metadata['gas_conditions']:
        for symbol in cfg['study']['species']:
            key=f"{condition['medium']}_{symbol}"
            dest=out/'raw/srmodule'/key
            dest.mkdir(parents=True,exist_ok=True)
            output=dest/'SRModule-output.txt'
            if not output.exists():
                module=runtime/'SR Module'; native=module/'CYGNO_OUTPUT.txt'
                native.unlink(missing_ok=True)
                ion=replace(elements[symbol],mass_u=ISOTOPE_MASSES[symbol])
                stopping.write_sr_in(module/'SR.IN',native.name,ion,as_gas(condition),elements,0.05,20000.,cfg['simulation']['compound_correction'])
                normalize_generated_crlf(module/'SR.IN')
                start=time.monotonic()
                core.launch_windows_executable(module/'SRModule.exe',module,cfg['simulation']['srmodule_timeout_s'],native)
                if not native.exists(): raise RuntimeError('SRModule output missing')
                shutil.copy2(native,output);shutil.copy2(module/'SR.IN',dest/'SR.IN')
                core.atomic_write_json(dest/'manifest.json',{'medium':condition['medium'],'species':symbol,'elapsed_s':time.monotonic()-start,'output_sha256':core.sha256_file(output)})
                print('SRModule complete',key,flush=True)
            for value in stopping.parse_stopping_output(output):
                rows.append({'medium':condition['medium'],'species':symbol,'energy_kevnr':value.energy_kev,
                    'electronic_stopping_mev_mg_cm2':value.s_elec,'nuclear_stopping_mev_mg_cm2':value.s_nuc,
                    'projected_range_mm':value.projected_range_cm*10,'longitudinal_straggling_mm':value.longitudinal_straggling_cm*10,
                    'lateral_straggling_mm':value.lateral_straggling_cm*10,'density_g_cm3':condition['density_g_cm3'],
                    'electronic_stopping_kev_mm':value.s_elec*condition['density_g_cm3']*1e5,
                    'nuclear_stopping_kev_mm':value.s_nuc*condition['density_g_cm3']*1e5,'method':'SRModule table; deterministic stopping prediction, not full-cascade QF'})
    core.write_csv_atomic(out/'srmodule.csv',rows)
    return rows

def finalize_jobs(jobs,cfg):
    result=[]
    for original in jobs:
        job=dict(original)
        semantic={key:job[key] for key in ['stage','medium','species','energy_kevnr','repeat','n_ions','displacement_energy_ev','exyz_energy_samples']}
        seed_payload={key:value for key,value in semantic.items() if key not in ['medium','exyz_energy_samples']}
        seed_payload['seed_base']=cfg['simulation']['random_seed_base']
        job['seed']=10000+int(hashlib.sha256(core.canonical_json(seed_payload).encode()).hexdigest()[:12],16)%890000000
        job['job_id']=core.stable_id('cygno',{**semantic,'seed':job['seed']})
        result.append(job)
    return result

def make_jobs(cfg,stage,out):
    sim=cfg['simulation']; grid=cfg['grid']; jobs=[]
    energies=grid['pilot_energies_kevnr'] if stage=='pilot' else log_grid(grid['energy_min_kevnr'],grid['energy_max_kevnr'],grid['energy_points'])
    repeats=1 if stage=='pilot' else sim['independent_batches']
    n=grid['pilot_ions'] if stage=='pilot' else sim['ions_per_batch']
    for symbol in cfg['study']['species']:
        for energy in energies:
            for repeat in range(repeats):
                for medium in ['baseline','contaminated']:
                    job={'stage':stage,'medium':medium,'species':symbol,'energy_kevnr':energy,'repeat':repeat,'n_ions':n,
                         'displacement_energy_ev':sim['displacement_energy_ev'],'exyz_energy_samples':sim['exyz_energy_samples']}
                    jobs.append(job)
    if stage=='systematics':
        jobs=[]
        for symbol in cfg['study']['species']:
            for energy in cfg['systematics']['energies_kevnr']:
                for threshold in cfg['systematics']['displacement_energies_ev']:
                    for repeat in range(cfg['systematics']['independent_batches']):
                        jobs.append({'stage':stage,'medium':'baseline','species':symbol,'energy_kevnr':energy,'repeat':repeat,
                          'n_ions':cfg['systematics']['ions_per_batch'],'displacement_energy_ev':threshold,'exyz_energy_samples':sim['exyz_energy_samples']})
                for refine in cfg['systematics']['exyz_refinement_factors']:
                    for repeat in range(cfg['systematics']['independent_batches']):
                        jobs.append({'stage':'track_sampling','medium':'baseline','species':symbol,'energy_kevnr':energy,'repeat':repeat,
                          'n_ions':cfg['systematics']['ions_per_batch'],'displacement_energy_ev':sim['displacement_energy_ev'],'exyz_energy_samples':sim['exyz_energy_samples']*refine})
    return finalize_jobs(jobs,cfg)

def invoke_native_trim(runtime,out,job,timeout):
    helper=ROOT/'scripts/cygno_native_run.py'
    if os.name=='nt':
        executable=sys.executable
        convert=lambda path:str(path.resolve())
    else:
        executable=os.environ.get('CYGNO_WINDOWS_PYTHON')
        if not executable:
            candidates=sorted(Path('/mnt/c/Users').glob('*/AppData/Roaming/uv/python/cpython-3.13*-windows-x86_64-none/python.exe'))
            if not candidates:raise RuntimeError('Set CYGNO_WINDOWS_PYTHON to a native Windows Python executable')
            executable=str(candidates[-1])
        convert=stopping.to_windows_path
    log=out/'native_logs'/f"{job['job_id']}.json"
    command=[str(executable),'-B',convert(helper),'--exe',convert(runtime/'TRIM.exe'),'--cwd',convert(runtime),
        '--timeout',str(timeout),'--ions',str(job['n_ions']),'--log',convert(log)]
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout+30)
    if result.returncode:
        raise RuntimeError('Native TRIM supervisor failed: '+result.stderr.decode('utf-8',errors='replace')[-3000:])
    return log

def run_one(job,runtime,out,cfg,metadata,elements):
    result_path=out/'batches'/f"{job['job_id']}.json"
    if result_path.exists(): return json.loads(result_path.read_text())
    condition=next(c for c in metadata['gas_conditions'] if c['medium']==job['medium'])
    table=stopping.parse_stopping_output(out/'raw/srmodule'/f"{job['medium']}_{job['species']}"/'SRModule-output.txt')
    prediction=stopping.interpolate_stopping(table,job['energy_kevnr'])
    width=max(1e4,cfg['simulation']['target_width_factor']*(prediction.projected_range_cm+cfg['simulation']['target_sigma_margin']*prediction.longitudinal_straggling_cm)*1e8)
    ion=replace(elements[job['species']],mass_u=ISOTOPE_MASSES[job['species']])
    start=time.monotonic()
    for attempt in range(3):
        core.clear_runtime_outputs(runtime)
        source_depth=width/2
        trim.write_trim_in(runtime/'TRIM.IN',as_gas(condition),ion,elements,job['energy_kevnr'],job['n_ions'],5,width,condition['density_g_cm3'],cfg['simulation']['compound_correction'],
          random_seed=job['seed'],save_3d_ranges=True,save_backscatter=True,save_transmit=True,displacement_energy_ev=job['displacement_energy_ev'],lattice_binding_energy_ev=cfg['simulation']['lattice_binding_energy_ev'],surface_binding_energy_ev=cfg['simulation']['surface_binding_energy_ev'])
        step=max(1,int(job['energy_kevnr']*1000/job['exyz_energy_samples']))
        normalize_generated_crlf(runtime/'TRIM.IN')
        energy_ceiling=physics.configure_bulk_energy_ceiling(runtime/'TRIM.IN',job['energy_kevnr'],relative_margin=job.get('bulk_energy_relative_margin',1e-5))
        physics.configure_trim_diskfiles(runtime/'TRIM.IN',exyz_step_ev=step,save_sputtered=True)
        # Shipped SRIMHelp/helpTR32.rtf identifies PlotType=5 as no plots.
        lines=(runtime/'TRIM.IN').read_text().splitlines()
        idx=next(i for i,line in enumerate(lines) if line.startswith('PlotType'))+1
        fields=lines[idx].split(); fields[0]='5';lines[idx]=' '.join(fields)
        trim.write_text_crlf(runtime/'TRIM.IN',lines)
        normalize_generated_crlf(runtime/'TRIM.IN')
        physics.write_bulk_trim_dat(runtime/'TRIM.dat',ion.z,job['energy_kevnr'],job['n_ions'],source_depth)
        trim.write_text_crlf(runtime/'TRIMAUTO',['1',''])
        normalize_generated_crlf(runtime/'TRIMAUTO')
        core.atomic_write_json(out/'progress'/f'{runtime.name}.json',{'status':'running','job':job,'started_at':core.utc_now(),'runtime':str(runtime)})
        native_log=invoke_native_trim(runtime,out,job,cfg['simulation']['trim_timeout_s'])
        outputs=core.collect_runtime_outputs(runtime)
        required=['IONIZ.TXT','PHONON.TXT','E2RECOIL.TXT','RANGE_3D.TXT','RANGE.TXT','TDATA.TXT']
        missing=[name for name in required if name not in outputs]
        if missing: raise RuntimeError(f'Missing full TRIM outputs: {missing}')
        tdata=outputs['TDATA.TXT'].read_text(encoding='latin-1')
        if 'Kinchin' in tdata: raise RuntimeError('Quick-cascade output cannot enter this study')
        endpoints=physics.parse_range_3d(outputs['RANGE_3D.TXT'],source_depth)
        if endpoints['n_records']!=job['n_ions']: raise RuntimeError(f"Endpoint count {endpoints['n_records']} != requested {job['n_ions']}")
        clipped=any(r['absolute_x_angstrom']<.02*width or r['absolute_x_angstrom']>.98*width for r in endpoints['records'])
        back=core.count_escape_records(outputs['BACKSCAT.TXT'],'B') if 'BACKSCAT.TXT' in outputs else 0
        transmitted=core.count_escape_records(outputs['TRANSMIT.TXT'],'T') if 'TRANSMIT.TXT' in outputs else 0
        if not clipped and not back and not transmitted: break
        if attempt<2: width*=2
    if clipped or back or transmitted: raise RuntimeError('Bulk source containment failed after 3 attempted widths')
    effective=physics.parse_effective_settings(runtime/'SRIM Restore/TDATA.sav')
    for setting in effective['target_energy_settings'].values():
        if abs(setting['lattice_binding_energy_ev'])>.01 or abs(setting['surface_binding_energy_ev'])>.01:
            raise RuntimeError('Native TRIM replaced gas binding settings with solid defaults')
    if effective['target_escape_count']>0:raise RuntimeError('Target secondary recoil escaped bulk gas window')
    if not math.isclose(effective['density_g_cm3'],condition['density_g_cm3'],rel_tol=5e-5):raise RuntimeError('Effective gas density disagrees with physical mixture')
    if effective['n_ions_calculated']!=job['n_ions'] or effective['cascade_type']!=5:
        raise RuntimeError('Effective TRIM histories/cascade mode disagree with requested job')
    budget=physics.integrate_energy_budget(outputs['IONIZ.TXT'],outputs['PHONON.TXT'],outputs['E2RECOIL.TXT'],job['energy_kevnr'],phonon_energy_convention='srim_documented_energy',vacancy_path=outputs.get('VACANCY.TXT'),effective_settings=effective)
    ioniz=trim.parse_ioniz(outputs['IONIZ.TXT'],job['energy_kevnr'])
    assert math.isclose(ioniz.qf_ioniz,budget['qf_electronic'],rel_tol=1e-4,abs_tol=1e-6)
    paths=physics.parse_exyz(outputs['EXYZ.TXT'],start_depth_angstrom=source_depth,endpoints=endpoints,initial_energy_kev=job['energy_kevnr']) if 'EXYZ.TXT' in outputs else None
    range_header=core.parse_range_summary(outputs['RANGE.TXT'])
    if paths:
        for track in paths['tracks']:track.pop('samples',None)
    result={'job':job,'runtime_s':time.monotonic()-start,'created_at':core.utc_now(),'budget':budget,
        'qf':budget['qf_electronic'],'eee_kev':budget['total_electronic_energy_kev'],'range_header':range_header.__dict__,
        'effective_settings':effective,'endpoints':endpoints,'paths':paths,'clipped':clipped,'backscattered':back,'transmitted':transmitted,
        'target_width_angstrom':width,'source_depth_angstrom':source_depth,'exyz_step_ev':step,'bulk_energy_ceiling':energy_ceiling,
        'source_revision':metadata.get('execution_source_revision')}
    archive=out/'raw'/job['stage']/f"{job['job_id']}.tar.gz"
    files={'TRIM.IN':runtime/'TRIM.IN','TRIM.dat':runtime/'TRIM.dat','TRIMAUTO':runtime/'TRIMAUTO','TRIM.cfg':runtime/'Data/TRIM.cfg','TDATA.sav':runtime/'SRIM Restore/TDATA.sav','native_process.json':native_log,**outputs}
    result['raw_archive']=str(archive.relative_to(out))
    effective['source']=str(archive.relative_to(out))+'::TDATA.sav'
    result['raw_sha256']={k:core.sha256_file(v) for k,v in files.items()}
    core.archive_files_atomic(archive,files,{'job':job,'source_depth_angstrom':source_depth,'runtime_s':result['runtime_s'],'sha256':result['raw_sha256'],'source_revision':result['source_revision']})
    core.atomic_write_json(result_path,result)
    core.atomic_write_json(out/'progress'/f'{runtime.name}.json',{'status':'complete','job':job,'runtime_s':result['runtime_s']})
    print(f"TRIM {job['stage']} {job['medium']} {job['species']} {job['energy_kevnr']:g} keVnr r{job['repeat']} N={job['n_ions']} QF={result['qf']:.5f} {result['runtime_s']:.1f}s",flush=True)
    return result

def execute(source,out,cfg,metadata,elements,stage,workers,limit=None,jobs_override=None,plan_name=None):
    jobs=make_jobs(cfg,stage,out) if jobs_override is None else finalize_jobs(jobs_override,cfg)
    plan=out/f'plan_{plan_name or stage}.json'
    previous=json.loads(plan.read_text()) if plan.exists() and jobs_override is not None else []
    merged={job['job_id']:job for job in previous+jobs}
    core.atomic_write_json(plan,list(merged.values()))
    pending=[j for j in jobs if not (out/'batches'/f"{j['job_id']}.json").exists()]
    if limit is not None: pending=pending[:limit]
    print(f'{stage}: {len(jobs)} planned, {len(pending)} pending; workers={workers}',flush=True)
    failures=[]
    def worker(index):
        runtime=runtime_for(source,out,f'worker-{index:03d}',cfg['simulation']['maximum_secondary_recoils'])
        for job in pending[index::workers]:
            if (out/'.stop-requested').exists():break
            try: run_one(job,runtime,out,cfg,metadata,elements)
            except Exception as error:
                record={'job':job,'error_type':type(error).__name__,'error':str(error),'created_at':core.utc_now()}
                try:
                    files={name:runtime/name for name in ['TRIM.IN','TRIM.dat','TRIMAUTO'] if (runtime/name).exists()}
                    files.update(core.collect_runtime_outputs(runtime))
                    core.archive_files_atomic(out/'raw/failures'/f"{job['job_id']}-{time.time_ns()}.tar.gz",files,record)
                except Exception as archive_error:record['failure_archive_error']=str(archive_error)
                core.atomic_write_json(out/'failures'/f"{job['job_id']}.json",record)
                core.atomic_write_json(out/'progress'/f'{runtime.name}.json',{'status':'failed',**record})
                failures.append(record)
                print('FAILED',job['job_id'],str(error),flush=True)
                # A broken runtime must not contaminate later jobs.
                break
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker,range(workers)))
    return not failures and not (out/'.stop-requested').exists()

def mean_sem(values):
    return (statistics.fmean(values),statistics.stdev(values)/math.sqrt(len(values)) if len(values)>1 else None) if values else (None,None)

def sd_and_jackknife(values):
    n=len(values)
    if n<2:return None,None
    mean=statistics.fmean(values);sse=math.fsum((x-mean)**2 for x in values)
    sd=math.sqrt(sse/(n-1))
    if n<3:return sd,None
    leave=[math.sqrt(max(0.,sse-n/(n-1)*(x-mean)**2)/(n-2)) for x in values]
    center=statistics.fmean(leave)
    return sd,math.sqrt((n-1)/n*math.fsum((x-center)**2 for x in leave))

def lateral_sd_and_jackknife(y,z):
    """Combined transverse dispersion, deleting each paired endpoint jointly."""
    if len(y)!=len(z):raise ValueError('Lateral coordinates must contain paired endpoints')
    n=len(y)
    if n<2:return None,None
    mean_y=statistics.fmean(y);mean_z=statistics.fmean(z)
    deviations=[(a-mean_y)**2+(b-mean_z)**2 for a,b in zip(y,z)]
    sse=math.fsum(deviations)
    sd=math.sqrt(sse/(2*(n-1)))
    if n<3:return sd,None
    leave=[math.sqrt(max(0.,sse-n/(n-1)*value)/(2*(n-2))) for value in deviations]
    center=statistics.fmean(leave)
    return sd,math.sqrt((n-1)/n*math.fsum((value-center)**2 for value in leave))

def analyse(out,cfg,source):
    batches=[json.loads(path.read_text()) for path in sorted((out/'batches').glob('*.json'))]
    def finite_number(value):
        return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    def complete_statistics(values):
        return mean_sem(values) if values and all(finite_number(value) for value in values) else (None,None)
    def valid_closure(batch):
        residual=batch['budget'].get('exclusive_energy_budget_with_binding_residual_fraction')
        return finite_number(residual) and abs(residual)<=cfg['validation']['maximum_energy_budget_relative_residual']
    stopping_tables={}
    for medium in ['baseline','contaminated']:
        for species in cfg['study']['species']:
            path=out/'raw/srmodule'/f'{medium}_{species}'/'SRModule-output.txt'
            if path.exists():stopping_tables[(medium,species)]=stopping.parse_stopping_output(path)
    groups={}
    for batch in batches:
        j=batch['job']
        if j['stage'] not in ['production','refinement']: continue
        groups.setdefault((j['medium'],j['species'],j['energy_kevnr']),[]).append(batch)
    rows=[];events=[]
    for (medium,species,energy),values in sorted(groups.items()):
        q,qs=mean_sem([b['qf'] for b in values]);ee,ees=mean_sem([b['eee_kev'] for b in values])
        endpoints=[r for b in values for r in b['endpoints']['records']]
        projections=[r['projected_displacement_mm'] for r in endpoints]
        summary=physics.distribution_summary(projections)
        y=[r['lateral_y_mm'] for r in endpoints];z=[r['lateral_z_mm'] for r in endpoints]
        path_values=[p['sampled_primary_polyline_length_mm'] for b in values if b['paths'] for p in b['paths']['tracks']]
        path_summary=physics.distribution_summary(path_values)
        row={'medium':medium,'species':species,'energy_kevnr':energy,'eee_srim_kev_mean':ee,'eee_srim_kev_sem':ees,
           'qf_mean':q,'qf_sem':qs,'n_ions_total':len(endpoints),'n_batches':len(values),
           'projected_range_mm_mean':summary['mean'],'projected_range_mm_sem':summary['sem'],
           'longitudinal_straggling_mm':summary['sample_sd'],
           'endpoint_radius_mm_mean':statistics.fmean(r['endpoint_displacement_3d_mm'] for r in endpoints),
           'primary_sampled_path_mm_mean':path_summary['mean'],'primary_sampled_path_mm_sem':path_summary['sem'],
           'actual_primary_track_length_mm':None,'total_cascade_track_length_mm':None,
           'clipped':any(b['clipped'] for b in values),'statistical_converged':False,
           'energy_budget_valid':all(valid_closure(b) for b in values),
           'physical_charge_qf':None,'physical_charge_eee_kev':None,'direct_simulation':True}
        for label,key in [
            ('primary_ionization_kev','primary_electronic_energy_kev'),
            ('secondary_ionization_kev','secondary_electronic_energy_kev'),
            ('phonon_kev','phonon_energy_kev'),
            ('binding_energy_sink_kev','binding_energy_sink_kev'),
            ('exclusive_energy_budget_residual_kev','exclusive_energy_budget_residual_kev'),
            ('exclusive_energy_budget_with_binding_residual_kev','exclusive_energy_budget_with_binding_residual_kev')]:
            row[label+'_mean'],row[label+'_sem']=complete_statistics([b['budget'].get(key) for b in values])
        row['remaining_energy_kev_mean'],row['remaining_energy_kev_sem']=complete_statistics([energy-b['eee_kev'] for b in values])
        closure_fractions=[b['budget'].get('exclusive_energy_budget_with_binding_residual_fraction') for b in values]
        closure_values=[1-value if finite_number(value) else None for value in closure_fractions]
        row['energy_closure_fraction_mean'],row['energy_closure_fraction_sem']=complete_statistics(closure_values)
        row['energy_budget_relative_residual_max_abs']=max(map(abs,closure_fractions)) if all(finite_number(value) for value in closure_fractions) else None
        transfers=[b['budget'].get('recoil_transfer_diagnostic_columns_kev') for b in values]
        row['recoil_transfer_diagnostic_column_count']=max((len(columns) for columns in transfers if isinstance(columns,list)),default=0)
        target_orders=[b.get('effective_settings',{}).get('target_element_order_z') for b in values]
        order=target_orders[0] if target_orders and all(value==target_orders[0] for value in target_orders) else None
        row['recoil_transfer_diagnostic_target_z_order']=json.dumps(order) if isinstance(order,list) and len(order)+1==row['recoil_transfer_diagnostic_column_count'] else None
        for column in range(row['recoil_transfer_diagnostic_column_count']):
            column_values=[columns[column] if isinstance(columns,list) and column<len(columns) else None for columns in transfers]
            label=f'recoil_transfer_diagnostic_column_{column+1}_kev'
            row[label+'_mean'],row[label+'_sem']=complete_statistics(column_values)
        row['recoil_transfer_diagnostic_interpretation']='Native energy-column order (1-based): first is energy from primary ions; remaining columns are energy absorbed by target elements in target_z_order. Intermediate transfer, excluded from terminal energy sum; columns are not summed'
        row['energy_channel_uncertainty_method']='SEM across independent batch means; closure residuals summarized per batch to retain channel covariance; missing channel values remain null'
        row['statistical_converged']=bool(len(values)>=cfg['simulation']['independent_batches'] and qs is not None and q>0 and qs/q<=cfg['validation']['maximum_qf_relative_sem'] and summary['mean']>0 and summary['sem'] is not None and summary['sem']/summary['mean']<=cfg['validation']['maximum_range_relative_sem'])
        row['longitudinal_straggling_mm_sem']=sd_and_jackknife(projections)[1]
        row['lateral_straggling_mm'],row['lateral_straggling_mm_sem']=lateral_sd_and_jackknife(y,z)
        row['lateral_y_straggling_mm'],row['lateral_y_straggling_mm_sem']=sd_and_jackknife(y)
        row['lateral_z_straggling_mm'],row['lateral_z_straggling_mm_sem']=sd_and_jackknife(z)
        row['straggling_uncertainty_method']='delete-one endpoint jackknife; sample dispersion separate from mean SEM'
        row['lateral_straggling_definition']='sqrt((sample variance y + sample variance z)/2); not SRIM absolute lateral projection statistic'
        row['endpoint_radius_mm_sem']=physics.distribution_summary([r['endpoint_displacement_3d_mm'] for r in endpoints])['sem']
        table=stopping_tables.get((medium,species))
        if table:
            prediction=stopping.interpolate_stopping(table,energy)
            row['electronic_stopping_mev_mg_cm2']=prediction.s_elec
            row['nuclear_stopping_mev_mg_cm2']=prediction.s_nuc
            row['stopping_statistical_uncertainty']=None
            row['stopping_method']='SRModule deterministic prediction, log interpolation; separate from direct full-cascade QF'
        rows.append(row)
        for b in values:
            path_map={p['ion_id']:p for p in b['paths']['tracks']} if b['paths'] else {}
            for record in b['endpoints']['records']:
                events.append({'medium':medium,'species':species,'energy_kevnr':energy,'batch':b['job']['repeat'],
                    **record,'primary_sampled_path_mm':path_map.get(record['ion_id'],{}).get('sampled_primary_polyline_length_mm')})
    if rows: core.write_csv_atomic(out/'summary.csv',rows)
    if events:
        import csv,io
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(events[0]));writer.writeheader();writer.writerows(events)
        with gzip.open(out/'events.csv.gz','wt',encoding='utf-8',newline='') as handle:handle.write(stream.getvalue())
    comparisons=[]
    indexed={(r['medium'],r['species'],r['energy_kevnr']):r for r in rows}
    for row in rows:
        if row['medium']!='baseline':continue
        wet=indexed.get(('contaminated',row['species'],row['energy_kevnr']))
        if not wet:continue
        base_batches=groups[('baseline',row['species'],row['energy_kevnr'])]
        wet_batches={b['job']['repeat']:b for b in groups[('contaminated',row['species'],row['energy_kevnr'])]}
        differences=[wet_batches[b['job']['repeat']]['qf']-b['qf'] for b in base_batches if b['job']['repeat'] in wet_batches]
        difference,sem=mean_sem(differences)
        range_differences=[wet_batches[b['job']['repeat']]['endpoints']['projected_range_mm']-b['endpoints']['projected_range_mm'] for b in base_batches if b['job']['repeat'] in wet_batches]
        range_difference,range_difference_sem=mean_sem(range_differences)
        t_factor={1:12.706,2:4.303,3:3.182,4:2.776,5:2.571,6:2.447,7:2.365,8:2.306}.get(len(differences)-1,1.96) if len(differences)>1 else None
        comparisons.append({'species':row['species'],'energy_kevnr':row['energy_kevnr'],'qf_difference':difference,'qf_difference_sem':sem,'range_difference_mm':range_difference,'range_difference_mm_sem':range_difference_sem,
            'range_relative_change_sem':range_difference_sem/row['projected_range_mm_mean'] if range_difference_sem is not None and row['projected_range_mm_mean']>0 else None,'confidence_t_factor':t_factor,
            'qf_relative_change':difference/row['qf_mean'] if difference is not None and row['qf_mean']>0 else None,'range_relative_change':(wet['projected_range_mm_mean']/row['projected_range_mm_mean']-1) if row['projected_range_mm_mean']>0 else None,
            'uncertainty_method':'paired independent-batch differences; shared seeds across media','n_pairs':len(differences),
            'impurity_equivalence_resolved':bool(sem is not None and row['qf_mean']>0 and (abs(difference)+t_factor*sem)/row['qf_mean']<cfg['validation']['impurity_material_relative_change'])})
    if comparisons:core.write_csv_atomic(out/'comparison.csv',comparisons)
    planned={job['job_id']:job for job in make_jobs(cfg,'production',out)}
    for name in ['plan_production.json','plan_refinement.json','plan_adaptive.json']:
        path=out/name
        if path.exists():planned.update({job['job_id']:job for job in json.loads(path.read_text())})
    expected=len(planned)
    completed=sum(b['job']['job_id'] in planned for b in batches)
    converged=sum(r['statistical_converged'] for r in rows)
    checks=[]
    def check(name,passed,detail,severity='failure'):
        checks.append({'check':name,'passed':bool(passed),'severity':'pass' if passed else severity,'detail':detail})
    check('production_complete',completed==expected,f'{completed}/{expected} full-cascade batches')
    check('statistical_convergence',bool(rows) and converged==len(rows),f'{converged}/{len(rows)} energy/species/medium points meet QF/range SEM targets')
    check('ioniz_energy_bounds',all(0<=b['qf']<=1.001 for b in batches),f'{len(batches)} actual batches checked')
    check('containment',not any(b['clipped'] or b['backscattered'] or b['transmitted'] for b in batches),'Bulk source and endpoint/escape checks')
    for medium in ['baseline','contaminated']:
        for species in cfg['study']['species']:
            curve=sorted([r for r in rows if r['medium']==medium and r['species']==species],key=lambda r:r['energy_kevnr'])
            check(f'coverage_{medium}_{species}',bool(curve) and min(r['eee_srim_kev_mean'] for r in curve)<=cfg['grid']['requested_eee_min_kev'] and max(r['eee_srim_kev_mean'] for r in curve)>=cfg['grid']['requested_eee_max_kev'],'Target 0.25-1000 SRIM-electronic keV; not calibrated charge keVee')
            check(f'monotonic_{medium}_{species}',bool(curve) and all(a['eee_srim_kev_mean']<b['eee_srim_kev_mean'] for a,b in zip(curve,curve[1:])),'Raw direct means; no forced monotone fit')
    metadata_path=out/'metadata.json'
    metadata=json.loads(metadata_path.read_text())
    metadata['expected_summary_rows']=len({(j['medium'],j['species'],j['energy_kevnr']) for j in planned.values()})
    core.atomic_write_json(metadata_path,metadata)
    original=json.loads((out/'provenance/source_installation.json').read_text())
    check('source_installation_preserved',original['fingerprint']==core.source_fingerprint(source) and original['control']==core.source_control_fingerprint(source),'Source installation fingerprint and mutable controls')
    check('physical_charge_calibration',False,'Unknown recoil-specific W/charge calibration; numerical physical charge QF intentionally null','limitation')
    check('total_3d_track_length',False,'EXYZ provides sampled primary paths; total primary/cascade path length not established','limitation')
    in_domain=[b for b in batches if b['eee_kev']>=cfg['grid']['requested_eee_min_kev']]
    low_energy=[b for b in batches if b['eee_kev']<cfg['grid']['requested_eee_min_kev']]
    check('terminal_energy_closure_requested_domain',bool(in_domain) and all(valid_closure(b) for b in in_domain),'Require 1% IONIZ+PHONON+binding energy closure where SRIM electronic surrogate is at least0.25keV; raw excursions retained')
    check('low_energy_control_budget',all(valid_closure(b) for b in low_energy),'Supplementary low-energy control points below requested0.25keV surrogate can show SRIM cutoff/transport overshoot; energy_budget_valid flags these rows','warning')
    from cygno_study_validation import analyse_systematics,verify_archives,compare_srmodule_media
    if (out/'srmodule.csv').exists():
        import csv
        with (out/'srmodule.csv').open(newline='',encoding='utf-8') as handle:
            medium_comparison=compare_srmodule_media(list(csv.DictReader(handle)),cfg['grid']['energy_min_kevnr'],cfg['grid']['energy_max_kevnr'],cfg['validation']['impurity_material_relative_change'])
        core.atomic_write_json(out/'srmodule_medium_comparison.json',medium_comparison)
        if medium_comparison['rows']:core.write_csv_atomic(out/'srmodule_medium_comparison.csv',medium_comparison['rows'])
        checks.extend(medium_comparison['checks'])
    systematic=analyse_systematics(batches,cfg)
    core.atomic_write_json(out/'systematics.json',systematic)
    if systematic['rows']:core.write_csv_atomic(out/'systematics.csv',systematic['rows'])
    checks.extend(systematic['checks'])
    archives=verify_archives(out,batches)
    core.atomic_write_json(out/'archive_validation.json',archives)
    checks.extend(archives['checks'])
    validation={'passed':completed==expected and all(c['passed'] or c['severity'] in ['limitation','warning'] for c in checks),'status':'complete' if completed==expected and all(c['passed'] or c['severity'] in ['limitation','warning'] for c in checks) else 'incomplete',
        'production_expected_batches':expected,'production_completed_batches':completed,'point_count':len(rows),'converged_points':converged,'checks':checks,'updated_at':core.utc_now()}
    core.atomic_write_json(out/'validation.json',validation)
    print(json.dumps({k:v for k,v in validation.items() if k!='checks'}),flush=True)
    return validation

def load_batches(out):
    return [json.loads(path.read_text()) for path in sorted((out/'batches').glob('*.json'))]

def campaign(source,out,cfg,metadata,elements,workers):
    import cygno_study_adaptive as adaptive
    with study_lock(out):
        if (out/'.stop-requested').exists():raise RuntimeError('Remove .stop-requested to resume the campaign')
        run_srmodule(source,out,cfg,metadata,elements)
        # Actual pilot histories establish coverage before production statistics.
        if not execute(source,out,cfg,metadata,elements,'pilot',workers):return False
        if (out/'.stop-requested').exists():return False
        if not execute(source,out,cfg,metadata,elements,'production',workers):return False
        analyse(out,cfg,source)
        if (out/'.stop-requested').exists():return False
        points=adaptive.refinement_points(load_batches(out),log_grid(cfg['grid']['energy_min_kevnr'],cfg['grid']['energy_max_kevnr'],cfg['grid']['energy_points']),
           requested_eee=(cfg['grid']['requested_eee_min_kev'],cfg['grid']['requested_eee_max_kev']))
        jobs=[]
        for species,energies in points.items():
            for energy in energies:
                for repeat in range(cfg['simulation']['independent_batches']):
                    for medium in ['baseline','contaminated']:
                        jobs.append({'stage':'refinement','species':species,'medium':medium,'energy_kevnr':energy,'repeat':repeat,
                           'n_ions':cfg['simulation']['ions_per_batch'],'displacement_energy_ev':cfg['simulation']['displacement_energy_ev'],
                           'exyz_energy_samples':cfg['simulation']['exyz_energy_samples']})
        core.atomic_write_json(out/'refinement_screening.json',{'proposed_points':points,'diagnostics':adaptive.refinement_diagnostics(load_batches(out),log_grid(cfg['grid']['energy_min_kevnr'],cfg['grid']['energy_max_kevnr'],cfg['grid']['energy_points']),(cfg['grid']['requested_eee_min_kev'],cfg['grid']['requested_eee_max_kev'])),'interpretation':'Proposed simulation energies, not interpolated results'})
        if jobs and not execute(source,out,cfg,metadata,elements,'refinement',workers,jobs_override=jobs):return False
        analyse(out,cfg,source)
        for iteration in range(cfg['simulation']['maximum_batches']):
            if (out/'.stop-requested').exists():return False
            batches=load_batches(out)
            jobs=adaptive.adaptive_repeats(batches,cfg)
            core.atomic_write_json(out/'convergence_diagnostics.json',adaptive.convergence_diagnostics(batches,cfg))
            if not jobs:break
            if not execute(source,out,cfg,metadata,elements,'production',workers,jobs_override=jobs,plan_name='adaptive'):return False
            analyse(out,cfg,source)
        if (out/'.stop-requested').exists():return False
        if not execute(source,out,cfg,metadata,elements,'systematics',workers):return False
        validation=analyse(out,cfg,source)
        from cygno_study_report import build_report
        artifacts=build_report(out)
        core.atomic_write_json(out/'campaign_completion.json',{'validation':validation,'artifacts':artifacts,'completed_at':core.utc_now(),
           'status':'complete' if validation['passed'] else 'finished_with_unresolved_validation'})
        return validation['passed']

@contextmanager
def study_lock(out):
    directory=out/'.simulation-lock'
    out.mkdir(parents=True,exist_ok=True)
    try:
        directory.mkdir()
    except FileExistsError:
        owner=json.loads((directory/'owner.json').read_text()) if (directory/'owner.json').exists() else {}
        raise RuntimeError(f"Study is already locked: {owner}. Verify no active native TRIM before removing stale lock.")
    core.atomic_write_json(directory/'owner.json',{'pid':os.getpid(),'host':socket.gethostname(),'started_at':core.utc_now()})
    try:yield
    finally:
        (directory/'owner.json').unlink(missing_ok=True)
        directory.rmdir()

def run_campaign(source,out,cfg,metadata,elements,workers):
    """Record terminal state even when a detached campaign fails."""
    state={'pid':os.getpid(),'host':socket.gethostname(),'status':'running','started_at':core.utc_now(),
           'source_revision':metadata.get('execution_source_revision')}
    core.atomic_write_json(out/'campaign_state.json',state)
    try:
        success=campaign(source,out,cfg,metadata,elements,workers)
        state['status']='complete' if success else ('stopped' if (out/'.stop-requested').exists() else 'failed')
        return success
    except BaseException as error:
        state.update(status='failed',error_type=type(error).__name__,error=str(error))
        raise
    finally:
        state['finished_at']=core.utc_now()
        core.atomic_write_json(out/'campaign_state.json',state)

def study_status(out):
    counts={}
    for path in (out/'batches').glob('*.json'):
        stage=json.loads(path.read_text())['job']['stage'];counts[stage]=counts.get(stage,0)+1
    state_path=out/'campaign_state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {}
    active=False
    if state.get('host')==socket.gethostname() and state.get('pid'):
        proc=Path(f"/proc/{state['pid']}/cmdline")
        if proc.exists():
            try:
                argv=proc.read_bytes().split(b'\0')
                active=(os.fsencode(str(Path(__file__).resolve())) in argv and b'campaign' in argv
                        and os.fsencode(str(out.resolve())) in argv)
            except OSError:pass
    if state.get('status')=='running' and not active:state={**state,'status':'interrupted_or_process_missing'}
    return {'campaign':state,'campaign_process_active':active,'completed':counts,
            'historical_failures':len(list((out/'failures').glob('*.json'))),
            'progress':[json.loads(p.read_text()) for p in sorted((out/'progress').glob('*.json'))]}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['plan','srmodule','pilot','run','systematics','campaign','analyse','report','status'])
    parser.add_argument('--config',type=Path,default=ROOT/'configs/cygno_hecf4_900mbar.toml')
    parser.add_argument('--run-dir',type=Path,default=ROOT/'runs/cygno_hecf4_900mbar_20261015')
    parser.add_argument('--srim-root',type=Path,default=ROOT)
    parser.add_argument('--workers',type=int)
    parser.add_argument('--limit',type=int)
    args=parser.parse_args();cfg=load_configuration(args.config);out=args.run_dir.resolve();source=args.srim_root.resolve()
    if args.command=='status':
        print(json.dumps(study_status(out),indent=2))
        return 0
    metadata,elements=prepare(out,cfg,args.config,source)
    if args.workers is not None and args.workers<1:parser.error('--workers must be positive')
    workers=args.workers or cfg['simulation']['workers']
    if args.command=='campaign':
        return 0 if run_campaign(source,out,cfg,metadata,elements,workers) else 1
    if args.command=='plan':
        for condition in metadata['gas_conditions']: print(json.dumps(condition,indent=2))
        print('Production batches:',len(make_jobs(cfg,'production',out)))
    elif args.command=='srmodule':
        with study_lock(out):run_srmodule(source,out,cfg,metadata,elements)
    elif args.command in ['pilot','run','systematics']:
        stage='production' if args.command=='run' else args.command
        with study_lock(out):success=execute(source,out,cfg,metadata,elements,stage,workers,args.limit)
        analyse(out,cfg,source)
        if not success:return 1
    elif args.command in ['analyse','report']:
        analyse(out,cfg,source)
        if args.command=='report':
            from cygno_study_report import build_report
            print(json.dumps(build_report(out),indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
