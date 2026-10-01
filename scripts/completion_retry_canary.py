#!/usr/bin/env python3
"""One-ion integration check of the completion monitor recovery; five-minute cap."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts import bulk_baseline_rebuild as b
from scripts import mode5_lifecycle_probe as observation
from scripts import trim_dat_completion_retry as worker
from scripts.staged_scan import write_json
from scripts.timing_pilot import resource_sample,GIB
scan=b.scan
RUN=ROOT/'runs/completion_retry_canary'
UNIT='srim-completion-retry-canary.service'
FAILED=b.RUN/'cases/Xe_F_e1000_n95_p1_b0/result.json'


def prepare():
    row=json.loads(FAILED.read_text())
    if row['status']!='FAILED' or 'Bad Window' not in row['error']:raise ValueError('Unexpected preserved failure')
    for path,digest in row['artifact_sha256'].items():
        if scan.sha256_file(Path(path))!=digest:raise ValueError('Failed raw evidence changed')
    source=Path(row['attempt'])
    content,events=observation.one_ion_inputs((source/'TRIM.IN').read_bytes(),(source/'TRIM.DAT').read_bytes())
    paths=[Path(__file__),Path(worker.__file__),Path(worker.original.__file__),FAILED]
    plan=dict(source=str(source),ions=1,budget_seconds=300,input_sha256=hashlib.sha256(content).hexdigest(),
              events_sha256=hashlib.sha256(events).hexdigest(),code_sha256={str(p):scan.sha256_file(p) for p in paths},
              note='One diagnostic history from failed Xe/F1MeV input; not production statistics. Normal exit, '
              'exact single acknowledgement, all final outputs, parameters and contained endpoint required.')
    path=RUN/'plan.json'
    if path.exists() and json.loads(path.read_text())!=plan:raise ValueError('Frozen canary changed')
    if not path.exists():write_json(path,plan)
    return plan,content,events,row


def run():
    group=Path(resource_sample()['cgroup'])
    if group.name!=UNIT or len(os.sched_getaffinity(0))>1 or (group/'memory.max').read_text().strip()!=str(8*GIB) or (group/'memory.swap.max').read_text().strip()!='0':
        raise RuntimeError('Use one-core, 8GiB, zero-swap canary service')
    if (RUN/'result.json').exists():raise RuntimeError('Canary already finished')
    plan,content,events,row=prepare()
    config=scan.load_config(b.base.d.CONFIG);context=scan.resolve_execution_context(config)
    source=scan.resolve_srim_root(None,config);before=scan.source_control_fingerprint(source)
    runtime=scan.prepare_runtimes(source,RUN,1,context)[0][0];origin=Path(plan['source'])
    scan.clear_runtime_outputs(runtime)
    (runtime/'TRIM.IN').write_bytes(content);(runtime/'TRIM.dat').write_bytes(events)
    (runtime/'TRIMAUTO').write_bytes((origin/'TRIMAUTO').read_bytes())
    (runtime/'Data/TRIM.cfg').write_bytes((origin/'TRIM.cfg').read_bytes())
    env=scan.pysrim_environment(scan.runtime_execution_context(context,RUN,runtime))
    start=time.monotonic();result=dict(status='RUNNING',started_at=datetime.now().astimezone().isoformat())
    write_json(RUN/'status.json',result)
    try:
        scan.launch_command([sys.executable,str(b.p.prior.display.__file__),'--runtime',str(runtime),'--',
                             sys.executable,str(worker.__file__),'--runtime',str(runtime),'--name','completion-retry-one-ion'],
                            runtime,240,environment=env,process_group=True)
        if (runtime/'TRIM.IN').read_bytes()!=content or (runtime/'TRIM.dat').read_bytes()!=events:raise ValueError('Input changed')
        outputs=scan.collect_runtime_outputs(runtime)
        for name in (*scan.STANDARD_TRIM_OUTPUTS,'RANGE_3D.TXT','BACKSCAT.TXT','TRANSMIT.TXT'):scan.find_output(outputs,name)
        data=scan.find_output(outputs,'TDATA.TXT').read_text(encoding='latin1')
        count=re.search(r'Total Ions calculated\s*=\s*([\d.]+)',data)
        if count is None or float(count[1])!=1 or not scan.check_target_energies(data,row['expected'])['passed']:
            raise ValueError('Canary count or binding mismatch')
        if not re.search(r'Ion\s*=\s*F\s*\(\s*9\)',data) or not re.search(r'Energy\s*=\s*1000(?:\.0*)?\s*keV',data):
            raise ValueError('Canary primary mismatch')
        back=scan.count_escape_records(scan.find_output(outputs,'BACKSCAT.TXT'),'B')
        transmit=scan.count_escape_records(scan.find_output(outputs,'TRANSMIT.TXT'),'T')
        endpoint=b.base.bulk.endpoints(scan.find_output(outputs,'RANGE_3D.TXT').read_text(encoding='latin1'),1,0,
                                      row['width_angstrom'],row['origin_angstrom'])
        if back or transmit:raise ValueError('Canary primary escaped')
        q=scan.parse_ioniz(scan.find_output(outputs,'IONIZ.TXT'),1000.)
        if not 0<q.qf_ioniz<=1:raise ValueError('Invalid canary ionization')
        ack=json.loads((runtime/'completion-ack.json').read_text());progress=json.loads((runtime/'.trim-progress.json').read_text())
        if ack['status']!='sent' or ack['title']!=worker.original.TITLE or progress['status']!='complete':raise ValueError('Missing normal completion')
        if not (runtime/'.completion-probes.json').exists():raise ValueError('Missing new monitor evidence')
        prepare()
        if scan.source_control_fingerprint(source)!=before:raise ValueError('Source controls changed')
        result.update(status='COMPLETE',ions_confirmed=1,ionization_fraction=q.qf_ioniz,**endpoint)
    except Exception as exc:result.update(status='NEEDS_ATTENTION',error=str(exc))
    finally:
        for option in ('-k','-w'):
            try:subprocess.run(['wineserver',option],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,check=False)
            except (OSError,subprocess.TimeoutExpired):pass
        out=RUN/'artifacts';out.mkdir(exist_ok=True)
        for _,path in scan.collect_runtime_outputs(runtime).items():shutil.copy2(path,out/path.name)
        names=['TRIM.IN','TRIM.dat','TRIMAUTO','.trim-progress.json','.trim-process.log','.xvfb.log',
               'completion-ack.json','.completion-probes.json','.display-startup.json']
        names+=[x.name for x in runtime.glob('*-screen.png')]+[x.name for x in runtime.glob('*-window-tree.txt')]
        for name in names:
            if (runtime/name).exists():shutil.copy2(runtime/name,out/name)
        shutil.copy2(runtime/'Data/TRIM.cfg',out/'TRIM.cfg')
        result.update(wall_seconds=time.monotonic()-start,source_unchanged=scan.source_control_fingerprint(source)==before,
                      resources=resource_sample(),artifact_sha256={str(x):scan.sha256_file(x) for x in out.iterdir() if x.is_file()})
        write_json(RUN/'result.json',result);write_json(RUN/'status.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='artifact_sha256'},indent=2))
    return 0 if result['status']=='COMPLETE' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=('prepare','run'));args=parser.parse_args()
    if args.command=='prepare':print(json.dumps(prepare()[0],indent=2))
    else:raise SystemExit(run())
