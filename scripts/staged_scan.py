#!/usr/bin/env python3
"""Detached, resource-capped commissioning followed by a gated production scan."""
from __future__ import annotations
import argparse
import csv
from datetime import datetime
import fcntl
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import srim_scan
from srim_gas_sweep import load_elements
from scripts.preliminary_report import atomic_text
from scripts.timing_pilot import completed_rows, memory_info, resource_sample, GIB
from scripts.staged_report import publish

RUN=ROOT/"runs/noble_cf4_staged"
KR_CONFIG=ROOT/"configs/noble_cf4_staged_kr.toml"
PRODUCTION_CONFIG=ROOT/"configs/noble_cf4_production.toml"
UNIT="srim-staged-scan.service"


def write_json(path,value):
    atomic_text(path,json.dumps(value,indent=2)+"\n")


def configured_jobs(config):
    cfg=srim_scan.load_config(config)
    elements,_=load_elements(srim_scan.resolve_srim_root(None,cfg)/"Data/ATOMDATA")
    conditions=srim_scan.generate_conditions(cfg,elements)
    return srim_scan.make_srmodule_jobs(conditions,cfg),srim_scan.make_trim_jobs(conditions,cfg)


def commissioning_jobs(jobs):
    selected=[]
    for job in jobs:
        p=job.payload; c=p["condition"]
        if p["repeat"]!=0 or c["pressure_atm"]!=1 or p["ion"]!=c["family"]:
            continue
        energy=round(p["energy_kev"])
        if (c["noble_percent"]==95 and energy in (10,100,1000)) or (c["noble_percent"]==60 and energy==1000):
            selected.append(job)
    if len(selected)!=12:
        raise ValueError(f"Expected 12 commissioning batches, found {len(selected)}")
    return selected


def validate_selected(run,jobs):
    rows,_=completed_rows(run)
    mapping={r["job_id"]:r for r in rows}
    issues=[]
    for job in jobs:
        r=mapping.get(job.job_id)
        if r is None:
            issues.append([job.job_id,"required batch not complete"])
            continue
        if r["n_ions"]!=job.payload["n_ions"] or r["repeat"]!=job.payload["repeat"]:
            issues.append([job.job_id,"history/repeat mismatch"])
        if r["random_seed"]!=srim_scan.seed_for(job.job_id,job.payload["repeat"]):
            issues.append([job.job_id,"seed mismatch"])
        if (r["clipped"] or not 0<=r["qf_trim_ionization"]<=1.000001
                or not math.isfinite(r["projected_range_mm"]) or r["projected_range_mm"]<=0
                or abs(r["total_ionization_energy_kev"]-r["energy_kev"]*r["qf_trim_ionization"])>1e-5):
            issues.append([job.job_id,"numeric bounds, energy identity or clipping"])
        for error in (
            srim_scan.validate_raw_archive(run/r["raw_archive"],job.job_id,"trim",("TRIM.IN","TRIMAUTO",*srim_scan.STANDARD_TRIM_OUTPUTS)),
            srim_scan.validate_profile_artifact(run/r["normalized_profiles"],job.job_id)):
            if error: issues.append([job.job_id,error])
    return issues


def forecast(small,large,jobs):
    """Conservative planning model, not a statistical runtime confidence bound."""
    reference={(r["family"],int(r["noble_percent"]),r["ion"],round(float(r["energy_kev"]))):
               float(r["wall_seconds_last_attempt"]) for r in small}
    correction={}
    for r in large:
        key=(r["family"],r["noble_percent"],r["ion"],round(r["energy_kev"]))
        five=reference[key]
        predicted=8+max(1,five-8)*r["n_ions"]/5
        correction[r["family"]]=max(correction.get(r["family"],1),r["wall_seconds_last_attempt"]/predicted)
    costs=[]
    for job in jobs:
        p=job.payload;c=p["condition"]
        energy=round(p["energy_kev"])
        low=reference[c["family"],60,p["ion"],energy]
        high=reference[c["family"],95,p["ion"],energy]
        w=(c["noble_percent"]-60)/35
        five=low*(1-w)+high*w
        costs.append((8+max(1,five-8)*p["n_ions"]/5)*correction.get(c["family"],1))
    workers=[0.]*15
    for value in sorted(costs,reverse=True):
        heapq.heapreplace(workers,workers[0]+value)
    return dict(model_hours=max(workers)/3600, planning_hours=1.5*max(workers)/3600,
                longest_batch_hours=max(costs)/3600, family_scaling_corrections=correction,
                assumptions="8 s startup; remaining cost scales with histories; measured conservative family corrections; linear mixture interpolation; unchanged pressure cost; 15-worker scheduling; 50% planning margin")


class Campaign:
    def __init__(self,run):
        self.run=run;self.child=None;self.stopping=False;self.started=time.monotonic();self.last_report=-300.
        self.state=dict(status="STARTING",phase="initialization",started_at=datetime.now().astimezone().isoformat())

    def stop(self,signum,frame):
        self.stopping=True
        if self.child is not None and self.child.poll() is None:self.child.terminate()

    def update(self,force=False):
        self.state.update(updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          session_elapsed_seconds=round(time.monotonic()-self.started,1),resources=resource_sample())
        sample=self.state["resources"]
        with (self.run/"resource_samples.csv").open("a",newline="") as output:
            writer=csv.DictWriter(output,fieldnames=list(sample))
            if output.tell()==0:writer.writeheader()
            writer.writerow(sample)
        write_json(self.run/"status.json",self.state)
        if force or time.monotonic()-self.last_report>120:
            publish(self.run,self.state)
            self.last_report=time.monotonic()

    def phase(self,name,config,run,stage,selected=None):
        if self.stopping:raise RuntimeError("operator requested stop")
        self.state.update(status="RUNNING",phase=name)
        command=[str(ROOT/"scripts/run_ubuntu_7day.sh"),str(run),"--config",str(config),"--stage",stage,"--workers","15"]
        for job in selected or []:command.extend(["--job-id",job.job_id])
        print(f"Starting phase: {name}",flush=True)
        self.child=subprocess.Popen(command,cwd=ROOT)
        self.update(force=True)
        while self.child.poll() is None:
            time.sleep(5)
            self.update()
        code=self.child.returncode;self.child=None
        if self.stopping:raise RuntimeError("operator requested stop")
        if code:raise RuntimeError(f"{name} exited {code}; inspect logs; no subsequent phase launched")
        self.update(force=True)

    def execute(self):
        self.run.mkdir(parents=True,exist_ok=True)
        sample=resource_sample();group=Path(sample["cgroup"])
        if len(os.sched_getaffinity(0))>15 or group.name!=UNIT:
            raise RuntimeError("Use staged_scan.sh start to enforce the CPU and memory limits")
        if (group/"memory.max").read_text().strip()!=str(32*GIB) or (group/"memory.swap.max").read_text().strip()!="0":
            raise RuntimeError("Resource controls do not match the approved limits")
        if memory_info()["MemAvailable"]<16*GIB:raise RuntimeError("Less than 16 GiB available RAM; defer launch")
        manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (KR_CONFIG,PRODUCTION_CONFIG)}
        frozen=self.run/"configuration_hashes.json"
        if frozen.exists() and json.loads(frozen.read_text())!=manifest:raise RuntimeError("Frozen staged configurations changed; use a new run")
        write_json(frozen,manifest)
        write_json(self.run/f"session-{os.getpid()}.json",dict(configs=manifest,started_at=self.state["started_at"],
                   cpu_affinity=sorted(os.sched_getaffinity(0)),code_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()))
        for signum in (signal.SIGTERM,signal.SIGINT):signal.signal(signum,self.stop)
        try:
            _,kr_jobs=configured_jobs(KR_CONFIG)
            sr_jobs,all_jobs=configured_jobs(PRODUCTION_CONFIG)
            selected=commissioning_jobs(all_jobs)
            self.phase("Krypton: 24 five-ion timing points",KR_CONFIG,self.run/"krypton","all")
            issues=validate_selected(self.run/"krypton",kr_jobs)
            write_json(self.run/"krypton_validation.json",dict(issues=issues,expected=len(kr_jobs)))
            if issues:raise RuntimeError("Krypton validation failed; production remains gated")
            self.phase("Production stopping/range tables",PRODUCTION_CONFIG,self.run/"production","srmodule")
            self.phase("Commissioning: 12 fifty-ion batches",PRODUCTION_CONFIG,self.run/"production","trim",selected)
            issues=validate_selected(self.run/"production",selected)
            small,_=completed_rows(ROOT/"runs/noble_cf4_timing_pilot")
            kr,_=completed_rows(self.run/"krypton")
            large,_=completed_rows(self.run/"production")
            selected_ids={j.job_id for j in selected}
            estimate=forecast(small+kr,[r for r in large if r["job_id"] in selected_ids],all_jobs) if not issues else {}
            events=dict(line.split() for line in (group/"memory.events").read_text().splitlines())
            if estimate.get("planning_hours",math.inf)>60:issues.append(["timing","forecast plus margin exceeds 60 h within the 72 h production session"])
            if estimate.get("longest_batch_hours",math.inf)>4:issues.append(["timing","predicted batch exceeds the safe 4 h commissioning threshold"])
            if int(events.get("oom_kill",0)):issues.append(["memory","OOM kill recorded"])
            if memory_info()["MemAvailable"]<8*GIB:issues.append(["memory","less than 8 GiB host RAM available"])
            if int((group/"memory.swap.current").read_text()):issues.append(["memory","scan unexpectedly uses swap"])
            write_json(self.run/"gate.json",dict(passed=not issues,issues=issues,forecast=estimate,
                       checked_batches=len(selected),resources=resource_sample(),memory_events=events))
            if issues:raise RuntimeError("Commissioning gate did not pass; inspect gate.json before increasing scope")
            self.state["planning_hours_at_gate"]=round(estimate["planning_hours"],2)
            print(f"Commissioning gate PASSED. Planning estimate {estimate['planning_hours']:.1f} h; releasing production.",flush=True)
            self.phase("Production: 1080 points, two 50-ion batches each",PRODUCTION_CONFIG,self.run/"production","trim")
            issues=validate_selected(self.run/"production",all_jobs)
            write_json(self.run/"final_validation.json",dict(issues=issues,expected_batches=len(all_jobs),
                       note="Operational and artifact checks; precision/model accuracy are not certified by completion."))
            if issues:raise RuntimeError("Production incomplete or validation failed; completed batches are preserved")
            self.state.update(status="COMPLETE",phase="Final SRIM reports")
            self.update(force=True)
            publish(self.run,self.state,final=True)
            self.state["phase"]="All stages complete"
            self.update(force=True)
            print("All stages complete. Results: "+str(self.run/"report/index.html"),flush=True)
            return 0
        except Exception as exc:
            self.state.update(status="STOPPED" if self.stopping else "NEEDS_ATTENTION",error=str(exc))
            print(f"Campaign stopped: {exc}",file=sys.stderr,flush=True)
            self.update(force=True)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    subprocess.run(["systemctl","--user","--no-block","stop",UNIT],check=False)


def status(run):
    unit=subprocess.run(["systemctl","--user","show",UNIT,"-p","ActiveState","-p","SubState","-p","MemoryCurrent","-p","MemorySwapCurrent"],capture_output=True,text=True)
    print(unit.stdout.strip())
    path=run/"status.json"
    if path.exists():
        state=json.loads(path.read_text());print(json.dumps(state,indent=2))
        age=time.time()-datetime.fromisoformat(state["updated_at"]).timestamp()
        if state["status"]=="RUNNING" and (age>180 or "ActiveState=active" not in unit.stdout):
            print("WARNING: saved RUNNING state is stale or service is inactive; inspect the log.")
    for name in ("krypton","production"):
        path=run/name/"jobs.sqlite"
        if path.exists():
            connection=sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)
            try:print(name,connection.execute("SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status").fetchall())
            finally:connection.close()
    print("Report:",run/"report/index.html")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("run","status","report"))
    args=parser.parse_args()
    if args.command=="status":status(RUN);return 0
    if args.command=="report":publish(RUN,json.loads((RUN/"status.json").read_text()));return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/"supervisor.lock").open("w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return Campaign(RUN).execute()


if __name__=="__main__":raise SystemExit(main())
