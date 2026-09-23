#!/usr/bin/env python3
"""Isolated TRIM input-format canaries; no results enter production statistics."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import srim_scan as scan
import pysrim_runner
from scripts.repro_check import archive_data, binding_report
from scripts.staged_scan import write_json
from scripts.preliminary_report import atomic_text, table
from scripts.timing_pilot import resource_sample, memory_info, GIB

RUN = ROOT / 'runs/binding_format_probe'
UNIT = 'srim-binding-probe.service'
SOURCE = ROOT / 'runs/noble_cf4_precision/refinement/raw/trim/tr-49fa5aa7a768bb5c6695.tar.gz'
CONFIG = ROOT / 'configs/noble_cf4_precision.toml'
CASES = [('baseline', 'original', False)] + [
    (f'{kind}_{style}', style, kind == 'markers')
    for kind in ('zero', 'markers') for style in ('padded', 'double_space', 'comma')
] + [('markers_single_space', 'single_space', True)]
HEADERS = ('Individual target atom displacement energies (eV)',
           'Individual target atom lattice binding energies (eV)',
           'Individual target atom surface binding energies (eV)')


def expected_values(markers):
    rows = ((31.,32.,33.),(1.,3.,5.),(6.,7.,9.)) if markers else ((21.,21.,21.),(0.,0.,0.),(0.,0.,0.))
    return {symbol:dict(displacement=rows[0][i],lattice=rows[1][i],surface=rows[2][i])
            for i,symbol in enumerate(('C','F','Kr'))}


def input_variant(original, style, markers):
    if style == 'original':
        return original
    lines = original.decode('ascii').splitlines()
    values = expected_values(markers)
    for header, field in zip(HEADERS, ('displacement','lattice','surface')):
        index = lines.index(header) + 1
        numbers = [values[s][field] for s in ('C','F','Kr')]
        if style == 'padded':
            line = ''.join(f'{v:8.1f}' for v in numbers)
        else:
            separator = {'single_space':' ', 'double_space':'  ', 'comma':','}[style]
            line = ' ' + separator.join(str(v) for v in numbers)
        lines[index] = line
    return ('\r\n'.join(lines)+'\r\n').encode('ascii')


def prepare(run):
    original = archive_data(SOURCE,'TRIM.IN')
    inputs = {name: input_variant(original,style,markers) for name,style,markers in CASES}
    plan = dict(source_archive=str(SOURCE), source_sha256=scan.sha256_file(SOURCE),
                config_sha256=scan.sha256_file(CONFIG),
                cases=[dict(name=name,style=style,expected=expected_values(markers),input_sha256=hashlib.sha256(inputs[name]).hexdigest())
                       for name,style,markers in CASES],
                ions_per_case=50, seed=268673998, physical_point='Kr80/CF4-20, C recoil, 30 keV, 1 atm',
                note='Format/marker diagnostics only. Not independent production histories.')
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen binding-probe inputs changed')
    write_json(path,plan)
    return plan,inputs


def run_case(case, content, runtime, context):
    started=time.monotonic()
    directory=RUN/'cases'/case['name']
    directory.mkdir(parents=True,exist_ok=True)
    result_path=directory/'result.json'
    if result_path.exists():
        return json.loads(result_path.read_text())
    result=dict(name=case['name'],style=case['style'],expected=case['expected'],status='RUNNING',runtime=str(runtime))
    scan.atomic_write_text(runtime/'TRIM.IN', content.decode('ascii'))
    scan.atomic_write_text(runtime/'TRIMAUTO', archive_data(SOURCE,'TRIMAUTO').decode('ascii'))
    # Keep an immutable diagnostic copy even if the executable rewrites a control file.
    scan.atomic_write_text(directory/'TRIM.IN',content.decode('ascii'))
    env=scan.pysrim_environment(context)
    try:
        command=['xvfb-run','-a','-e',str(runtime/'.xvfb.log'),'-s','-screen 0 1024x768x24',
                 sys.executable,str(Path(__file__).resolve()),'worker','--runtime',str(runtime),'--name',case['name']]
        scan.launch_command(command,runtime,600,environment=env,process_group=True,display_name=case['name'])
        outputs=scan.collect_runtime_outputs(runtime)
        for name in ('RANGE.TXT','IONIZ.TXT','TDATA.TXT'):
            scan.find_output(outputs,name)
        observed=binding_report(scan.find_output(outputs,'TDATA.TXT').read_text(encoding='latin1'))
        ranges=scan.parse_range_summary(scan.find_output(outputs,'RANGE.TXT'))
        ioniz=scan.parse_ioniz(scan.find_output(outputs,'IONIZ.TXT'),30.)
        result.update(status='COMPLETE',observed=observed,parameters_match=observed==case['expected'],
                      projected_range_mm=ranges.projected_range_mm,ionization_fraction=ioniz.qf_ioniz,
                      input_unchanged=(runtime/'TRIM.IN').read_bytes()==content,
                      output_sha256={name:scan.sha256_file(scan.find_output(outputs,name)) for name in ('RANGE.TXT','IONIZ.TXT','TDATA.TXT')})
    except Exception as exc:
        result.update(status='FAILED',error=str(exc))
    finally:
        subprocess.run(['wineserver','-k'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=False)
        subprocess.run(['wineserver','-w'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=False)
    result['wall_seconds']=time.monotonic()-started
    write_json(result_path,result)
    return result


def publish(state, results):
    write_json(RUN/'status.json',state)
    write_json(RUN/'results.json',results)
    body='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><h1>TRIM binding-energy input-format probes</h1>'
    body+='<p>Eight isolated 50-ion canaries. Identical seed, geometry and recoil; only binding/displacement input rows are varied. Marker cases intentionally change parameters and are not physical production results.</p>'
    body+='<p><a href="results.json">Full results</a> · <a href="plan.json">Frozen inputs</a>. Production remains on hold.</p>'
    body+=table([state],list(state))
    body+=table(results,['name','status','parameters_match','observed','projected_range_mm','ionization_fraction','input_unchanged','wall_seconds'])
    atomic_text(RUN/'index.html',body)


def execute():
    group=Path(resource_sample()['cgroup'])
    if group.name!=UNIT or len(os.sched_getaffinity(0))>8:
        raise RuntimeError('Use binding_probe.sh for isolated resource limits')
    if (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0':
        raise RuntimeError('Expected 32 GiB hard RAM and zero scan swap')
    if memory_info()['MemAvailable']<16*GIB:
        raise RuntimeError('Insufficient available host memory')
    plan,inputs=prepare(RUN)
    cfg=scan.load_config(CONFIG)
    context=scan.resolve_execution_context(cfg)
    provenance=scan.execution_provenance(context)
    source=scan.resolve_srim_root(None,cfg)
    before=scan.source_control_fingerprint(source)
    # Separate runtime and Wine prefix for every variant. No production directory is a target.
    runtimes,fingerprint=scan.prepare_runtimes(source,RUN,len(CASES),context)
    write_json(RUN/'provenance.json',dict(execution=provenance,srim=fingerprint,installed_inputs=before,
               commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()))
    state=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat(),expected_cases=len(CASES))
    results=[]
    with ThreadPoolExecutor(max_workers=8) as pool:
        active={pool.submit(run_case,case,inputs[case['name']],runtime,scan.runtime_execution_context(context,RUN,runtime))
                for case,runtime in zip(plan['cases'],runtimes)}
        while active:
            done,active=wait(active,timeout=5)
            for future in done:
                result=future.result()
                results.append(result)
                print(json.dumps(result),flush=True)
            state.update(updated_at=datetime.now().astimezone().isoformat(),completed_cases=len(results),resources=resource_sample())
            publish(state,results)
    unchanged=scan.source_control_fingerprint(source)==before
    baseline=next(r for r in results if r['name']=='baseline')
    baseline_matches=baseline.get('status')=='COMPLETE' and all(baseline['output_sha256'][name]==hashlib.sha256(archive_data(SOURCE,name)).hexdigest()
                         for name in ('RANGE.TXT','IONIZ.TXT','TDATA.TXT'))
    state.update(status='DIAGNOSTICS_COMPLETE_REVIEW_REQUIRED',installed_inputs_unchanged=unchanged,baseline_reproduced=baseline_matches)
    prepare(RUN)
    publish(state,results)
    write_json(RUN/'completion.json',state)
    return 0 if unchanged and baseline_matches else 1


class ArchivedInput:
    def _write_input_files(self):
        pass  # Input was deliberately frozen by the parent, not regenerated by PySRIM.


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','run','worker','status'))
    parser.add_argument('--runtime',type=Path)
    parser.add_argument('--name')
    args=parser.parse_args()
    if args.command=='worker':
        pysrim_runner.configure_recoil_capacity(args.runtime)
        pysrim_runner.monitored_trim_run(ArchivedInput(),args.runtime,dict(description=args.name))
        return 0
    if args.command=='status':
        subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'])
        if (RUN/'status.json').exists():print((RUN/'status.json').read_text())
        print('Report:',RUN/'index.html')
        return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            plan,_=prepare(RUN)
            print(json.dumps(plan,indent=2))
            return 0
        return execute()


if __name__=='__main__':raise SystemExit(main())
