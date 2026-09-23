#!/usr/bin/env python3
"""Isolated same-seed Kr replays; never pooled into independent statistics."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.low_energy_topup import TopupCampaign
from scripts.staged_scan import configured_jobs, validate_selected, write_json
from scripts.precision_scan import pending, job_key
from scripts.preliminary_report import atomic_text, table
from scripts.timing_pilot import completed_rows, resource_sample, memory_info, GIB

RUN = ROOT / 'runs/kr_repro_check'
UNIT = 'srim-repro-check.service'
SOURCES = (
    ('old', ROOT/'configs/noble_cf4_precision.toml', ROOT/'runs/noble_cf4_precision/refinement'),
    ('new', ROOT/'configs/noble_cf4_mixture_refinement.toml', ROOT/'runs/noble_cf4_mixture_refinement/refinement'),
)
KEYS = {('Kr', n, 1., 'C', 30.) for n in (70, 80, 90)}


def archive_data(path, name):
    with tarfile.open(path) as archive:
        return archive.extractfile(name).read()


def binding_report(text):
    pattern = r'Target energies for target atom\s*=\s*(\w+)\s+Displacement\s*=\s*([\d.eE+-]+) eV, Binding\s*=\s*([\d.eE+-]+) eV, Surface\s*=\s*([\d.eE+-]+) eV'
    return {symbol: dict(displacement=float(d), lattice=float(l), surface=float(s))
            for symbol, d, l, s in re.findall(pattern, text)}


def prepare(run):
    groups = []
    hashes = {}
    for label, config, source in SOURCES:
        rows, _ = completed_rows(source)
        sr, jobs = configured_jobs(config)
        mapping = {j.job_id: j for j in jobs}
        records = [min((r for r in rows if (r['family'],r['noble_percent'],r['pressure_atm'],r['ion'],round(r['energy_kev'],9)) == key),
                       key=lambda r: r['repeat']) for key in sorted(KEYS)]
        selected = [mapping[r['job_id']] for r in records]
        pairs = {(j.payload['condition']['condition_id'],j.payload['ion']) for j in selected}
        stopping = [j for j in sr if (j.payload['condition']['condition_id'],j.payload['ion']) in pairs]
        hashes[str(config)] = hashlib.sha256(config.read_bytes()).hexdigest()
        hashes[str(source/'trim_results.csv')] = hashlib.sha256((source/'trim_results.csv').read_bytes()).hexdigest()
        for r in records:
            path = source/r['raw_archive']
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        groups.append((label,config,source,stopping,selected,records))
    plan = dict(input_hashes=hashes, replays={g[0]:[j.job_id for j in g[4]] for g in groups},
                note='Six diagnostic repeats only; never add these to SEM/history counts.', budget_hours=1)
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen replay inputs changed')
    write_json(path, plan)
    return groups


def compare(source, destination, original, replay):
    a, b = source/original['raw_archive'], destination/replay['raw_archive']
    names = ('TRIM.IN','TRIMAUTO','Data/TRIM.cfg','RANGE.TXT','IONIZ.TXT','TDATA.TXT')
    equality = {name: archive_data(a,name) == archive_data(b,name) for name in names}
    metrics = {field: dict(original=original[field], replay=replay[field], equal=original[field] == replay[field])
               for field in ('projected_range_mm','qf_trim_ionization','longitudinal_straggling_mm')}
    bindings = binding_report(archive_data(b,'TDATA.TXT').decode('latin1'))
    return dict(job_id=original['job_id'], noble_percent=original['noble_percent'], random_seed=original['random_seed'],
                file_equality=equality, metrics=metrics, reported_bindings=bindings,
                exact_reproduction=all(equality.values()) and all(v['equal'] for v in metrics.values()),
                configured_zero_bindings_confirmed=len(bindings)==3 and all(v['lattice']==0 and v['surface']==0 for v in bindings.values()))


class ReplayCampaign(TopupCampaign):
    @property
    def budget_limit(self):
        return 3600

    def publish_report(self):
        comparisons = json.loads((self.run/'comparison.json').read_text()) if (self.run/'comparison.json').exists() else []
        body = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="30"><h1>Kr identical-seed replay diagnostics</h1>'
        body += '<p>Diagnostic replays are excluded from all independent statistical estimates. Fresh production remains on hold pending replay and physical-input review.</p>'
        body += '<p><a href="comparison.json">Detailed comparisons</a> · <a href="plan.json">Frozen replay plan</a></p>'
        body += table([self.state], list(self.state))
        body += table(comparisons, ['cohort','job_id','noble_percent','exact_reproduction','configured_zero_bindings_confirmed'])
        atomic_text(self.run/'index.html',body)

    def selected(self, name, config, destination, stage, jobs):
        if not jobs:
            raise RuntimeError('Refusing empty diagnostic selection')
        todo = pending(destination,jobs)
        if todo:
            self.phase(name,config,destination,stage,todo)
        if pending(destination,jobs):
            raise RuntimeError('Diagnostic jobs incomplete')

    def execute(self):
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,self.stop)
        try:
            group = Path(resource_sample()['cgroup'])
            if group.name != UNIT or len(os.sched_getaffinity(0))>15:
                raise RuntimeError('Launch through repro_check.sh for resource limits')
            if (group/'memory.max').read_text().strip()!=str(32*GIB) or (group/'memory.swap.max').read_text().strip()!='0':
                raise RuntimeError('Expected 32 GiB memory and zero scan swap')
            if memory_info()['MemAvailable']<16*GIB or self.used>=self.budget_limit:
                raise RuntimeError('Insufficient memory/time budget')
            groups = prepare(self.run)
            comparisons=[]
            write_json(self.run/f'session-{os.getpid()}.json',dict(started_at=self.state['started_at'],
                       commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                       cpu_affinity=sorted(os.sched_getaffinity(0))))
            for label,config,source,sr,jobs,records in groups:
                issues=validate_selected(source,jobs)
                if issues:
                    raise RuntimeError(f'Original replay artifacts invalid: {issues[:3]}')
                destination=self.run/label
                self.selected(label+' stopping tables',config,destination,'srmodule',sr)
                self.selected(label+' same-seed TRIM replays',config,destination,'trim',jobs)
                issues=validate_selected(destination,jobs)
                if issues:
                    raise RuntimeError(f'Replay artifacts invalid: {issues[:3]}')
                rows,_=completed_rows(destination)
                replay={r['job_id']:r for r in rows}
                for record in records:
                    result=compare(source,destination,record,replay[record['job_id']])
                    result['cohort']=label
                    comparisons.append(result)
                write_json(self.run/'comparison.json',comparisons)
            prepare(self.run)
            self.state.update(status='DIAGNOSTICS_COMPLETE_REVIEW_REQUIRED',phase='Replay and physical-input checks complete',
                              exact_replays=sum(r['exact_reproduction'] for r in comparisons),expected_replays=6,
                              zero_binding_matches=sum(r['configured_zero_bindings_confirmed'] for r in comparisons))
            self.update(force=True)
            write_json(self.run/'completion.json',self.state)
            return 0
        except Exception as exc:
            self.state.update(status='BUDGET_EXHAUSTED' if self.budget_stopped else 'STOPPED' if self.stopping else 'NEEDS_ATTENTION',error=str(exc))
            self.update(force=True)
            print(str(exc),file=sys.stderr,flush=True)
            return 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=30)
                except subprocess.TimeoutExpired:subprocess.run(['systemctl','--user','--no-block','stop',UNIT],check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','run','status'))
    args=parser.parse_args()
    if args.command=='status':
        subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MemoryCurrent','-p','MemorySwapCurrent'])
        for name in ('status.json','completion.json'):
            if (RUN/name).exists():print((RUN/name).read_text())
        print('Report:',RUN/'index.html')
        return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='prepare':
            groups=prepare(RUN)
            print('Frozen replay IDs:',{g[0]:[j.job_id for j in g[4]] for g in groups})
            return 0
        return ReplayCampaign(RUN).execute()


if __name__=='__main__':raise SystemExit(main())
