#!/usr/bin/env python3
"""Bounded, resumable precision follow-up; original scan is read-only."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
import argparse
import csv
import fcntl
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.staged_scan import Campaign, configured_jobs, validate_selected, write_json
from scripts.timing_pilot import completed_rows, memory_info, resource_sample, GIB
from scripts import precision_analysis as analysis

RUN=ROOT/"runs/noble_cf4_precision"
BASELINE=ROOT/"runs/noble_cf4_staged/production"
CONFIG=ROOT/"configs/noble_cf4_precision.toml"
TIMING_CONFIG=ROOT/"configs/noble_cf4_precision_timing.toml"
UNIT="srim-precision-scan.service"
BUDGET=72*3600
OLD_ENERGIES={1,10,100,1000}


def initial_jobs(jobs,old):
    return [j for j in jobs if (round(j.payload["energy_kev"]) in OLD_ENERGIES)==old
            and j.payload["repeat"]<(4 if old else 6)]


def job_key(job):
    p=job.payload;c=p["condition"]
    return (c["family"],c["noble_percent"],c["pressure_atm"],p["ion"],round(p["energy_kev"],9))


def pending(run,jobs):
    database=run/"jobs.sqlite"
    if not database.exists():return jobs
    connection=sqlite3.connect(database.resolve().as_uri()+"?mode=ro",uri=True)
    try:records=connection.execute("SELECT job_id,result_path FROM jobs WHERE status='complete'").fetchall()
    finally:connection.close()
    complete={job for job,path in records if path and (run/path).exists()}
    return [j for j in jobs if j.job_id not in complete]


def cost_model(baseline,corrections=None):
    grouped=defaultdict(list)
    for row in baseline:grouped[analysis.key(row)].append(row["wall_seconds_last_attempt"])
    times={k:statistics.mean(v) for k,v in grouped.items()}
    def cost(job):
        k=job_key(job);energy=k[-1]
        if k in times:value=times[k]
        else:
            lo=max(e for e in OLD_ENERGIES if e<energy);hi=min(e for e in OLD_ENERGIES if e>energy)
            w=math.log(energy/lo)/math.log(hi/lo)
            value=8+math.exp((1-w)*math.log(max(1,times[k[:-1]+(lo,)]-8))+w*math.log(max(1,times[k[:-1]+(hi,)]-8)))
        return value*(corrections or {}).get(k[0],1)
    return cost


def adaptive_jobs(jobs,groups):
    result=[]
    for job in jobs:
        k=job_key(job);g=(k[0],k[1],k[3],k[4]);start=4 if round(k[4]) in OLD_ENERGIES else 6
        if g in groups and start<=job.payload["repeat"]<start+4:result.append(job)
    return result


class PrecisionCampaign(Campaign):
    def __init__(self,run):
        super().__init__(run)
        budget=run/"budget.json"
        self.used=float(json.loads(budget.read_text())["used_seconds"]) if budget.exists() else 0.
        self.tick=time.monotonic();self.budget_stopped=False

    def update(self,force=False):
        now=time.monotonic();self.used+=now-self.tick;self.tick=now
        if self.used>=BUDGET and not self.stopping:
            self.budget_stopped=True;self.stop(signal.SIGTERM,None)
        self.state.update(updated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                          cumulative_hours=round(self.used/3600,4),remaining_hours=max(0,round((BUDGET-self.used)/3600,4)),
                          resources=resource_sample())
        write_json(self.run/"budget.json",dict(used_seconds=self.used,limit_seconds=BUDGET))
        write_json(self.run/"status.json",self.state)
        sample=self.state["resources"]
        with (self.run/"resource_samples.csv").open("a",newline="") as handle:
            writer=csv.DictWriter(handle,fieldnames=list(sample))
            if handle.tell()==0:writer.writeheader()
            writer.writerow(sample)
        if force or now-self.last_report>300:
            analysis.publish(self.run,BASELINE,self.state)
            self.last_report=time.monotonic()

    def selected_phase(self,name,config,run,jobs,cost=None):
        if not jobs:return
        todo=pending(run,jobs)
        if not todo:
            print(f"{name}: all selected jobs already complete",flush=True)
        else:
            estimate=sum(cost(j) for j in todo)/15 if cost else 0
            if cost and 1.5*estimate+6*3600>BUDGET-self.used:
                raise RuntimeError(f"{name}: estimated work plus drain reserve exceeds remaining 72-hour budget")
            # Only a nonempty explicit selection can reach the generic runner.
            self.phase(name,config,run,"trim",todo)
        issues=validate_selected(run,jobs)
        if issues:
            write_json(self.run/"validation_failure.json",dict(phase=name,issues=issues))
            raise RuntimeError(f"{name}: output validation failed")

    def execute(self):
        sample=resource_sample();group=Path(sample["cgroup"])
        if len(os.sched_getaffinity(0))>15 or group.name!=UNIT:
            raise RuntimeError("Use precision_scan.sh start for enforced resource limits")
        if (group/"memory.max").read_text().strip()!=str(32*GIB) or (group/"memory.swap.max").read_text().strip()!="0":
            raise RuntimeError("Expected 32 GiB hard RAM and zero scan swap")
        if memory_info()["MemAvailable"]<16*GIB:raise RuntimeError("Need at least 16 GiB available host RAM")
        if self.used>=BUDGET:raise RuntimeError("Cumulative budget exhausted; review before allocating another campaign")
        sources=[CONFIG,TIMING_CONFIG,BASELINE/"trim_results.csv"]
        hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        manifest=self.run/"input_hashes.json"
        if manifest.exists() and json.loads(manifest.read_text())!=hashes:raise RuntimeError("Frozen inputs changed")
        write_json(manifest,hashes)
        write_json(self.run/f"session-{os.getpid()}.json",dict(started_at=self.state["started_at"],
                   commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
                   packages={p:importlib.metadata.version(p) for p in ("numpy","scipy","uproot","matplotlib")},
                   cpu_affinity=sorted(os.sched_getaffinity(0))))
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,self.stop)
        try:
            baseline,_=completed_rows(BASELINE)
            if len(baseline)!=2160:raise RuntimeError("Expected exactly 2160 completed baseline batches")
            _,baseline_jobs=configured_jobs(ROOT/"configs/noble_cf4_production.toml")
            if validate_selected(BASELINE,baseline_jobs):raise RuntimeError("Read-only baseline audit failed")
            _,timing_jobs=configured_jobs(TIMING_CONFIG)
            timing_jobs=[j for j in timing_jobs if j.payload["ion"]==j.payload["condition"]["family"]]
            _,jobs=configured_jobs(CONFIG)
            self.phase("100-ion timing: stopping tables",TIMING_CONFIG,self.run/"timing","srmodule")
            self.selected_phase("100-ion timing: six noble-recoil jobs",TIMING_CONFIG,self.run/"timing",timing_jobs)
            timing,_=completed_rows(self.run/"timing")
            corrections={};comparison=[]
            for row in timing:
                controls=[r for r in baseline if analysis.key(r)==analysis.key(row)]
                predicted=8+2*max(1,statistics.mean(r["wall_seconds_last_attempt"] for r in controls)-8)
                ratio=row["wall_seconds_last_attempt"]/predicted
                corrections[row["family"]]=max(1,corrections.get(row["family"],1),ratio)
                comparison.append(dict(point=list(analysis.key(row)),wall_seconds_100=row["wall_seconds_last_attempt"],
                                       predicted_from_50=predicted,ratio=ratio))
            events=dict(line.split() for line in (group/"memory.events").read_text().splitlines())
            if int(events.get("oom_kill",0)) or memory_info()["MemAvailable"]<8*GIB:raise RuntimeError("Resource gate failed")
            write_json(self.run/"timing_check.json",dict(comparisons=comparison,corrections=corrections,
                       note="Observed runtime comparison, not a controlled fixed-concurrency benchmark or guaranteed extrapolation."))
            cost=cost_model(baseline,corrections)
            initial=initial_jobs(jobs,True)+initial_jobs(jobs,False)
            todo=pending(self.run/"refinement",initial)
            estimate=sum(cost(j) for j in todo)/15/3600
            write_json(self.run/"initial_plan.json",dict(jobs=len(initial),remaining_jobs=len(todo),model_hours=estimate,
                       planning_hours=1.5*estimate,maximum_cumulative_hours=72,targets=analysis.TARGETS))
            if 1.5*estimate+6>(BUDGET-self.used)/3600:raise RuntimeError("Initial precision/energy grid exceeds remaining guarded budget")
            self.phase("Seven-energy stopping tables",CONFIG,self.run/"refinement","srmodule")
            self.selected_phase("Four-batch precision at original energies",CONFIG,self.run/"refinement",initial_jobs(jobs,True),cost)
            self.selected_phase("Four-batch coverage at 3, 30, 300 keV",CONFIG,self.run/"refinement",initial_jobs(jobs,False),cost)
            if len(analysis.points(analysis.read_rows(BASELINE,self.run)))!=1890:
                raise RuntimeError("Initial seven-energy grid is incomplete; adaptation is not allowed")
            adaptive_path=self.run/"adaptive_plan.json"
            if adaptive_path.exists():
                plan=json.loads(adaptive_path.read_text());ids=set(plan["job_ids"])
                selected=[j for j in jobs if j.job_id in ids]
                if len(selected)!=len(ids):raise RuntimeError("Frozen adaptive selection does not match configured jobs")
            else:
                rows=analysis.read_rows(BASELINE,self.run)
                groups=analysis.scientific_selection(rows)
                grouped=defaultdict(list)
                for j in adaptive_jobs(jobs,groups):
                    k=job_key(j);grouped[(k[0],k[1],k[3],k[4])].append(j)
                remaining=max(0,BUDGET-self.used-6*3600);selected=[];deferred=[];admitted_seconds=0
                # Select complete six-pressure groups, cheapest first. Decisions
                # are frozen before new samples are observed; no repeated peeking.
                for g,batch in sorted(grouped.items(),key=lambda pair:sum(cost(j) for j in pair[1])):
                    projected=1.5*sum(cost(j) for j in batch)/15
                    if admitted_seconds+projected<=remaining:
                        selected.extend(batch);admitted_seconds+=projected
                    else:deferred.append(list(g))
                plan=dict(job_ids=[j.job_id for j in selected],selected_groups=len(selected)//24,
                          deferred_groups=deferred,planning_hours=admitted_seconds/3600,
                          selection="One fixed four-batch look; range SEM >5%, ionization SEM >2%, or adjusted/indeterminate pressure test; whole pressure groups; cheapest first under budget")
                write_json(adaptive_path,plan)
            self.selected_phase("Adaptive independent confirmation: up to eight batches",CONFIG,self.run/"refinement",selected,cost)
            self.state.update(status="RUN_COMPLETE",phase="Final precision and pressure assessment")
            self.update(force=True)
            rows,combined,summary=analysis.publish(self.run,BASELINE,self.state,final=True)
            unresolved=[r for r in combined if not r["precision_met"]]
            self.state.update(status="REVIEW_REQUIRED" if unresolved or summary["independent_pressure_flags"] or plan["deferred_groups"] else "TARGETS_MET",
                              phase="Bounded campaign finished",unresolved_precision_points=len(unresolved),
                              deferred_pressure_groups=len(plan["deferred_groups"]))
            self.update(force=True)
            write_json(self.run/"completion.json",dict(status=self.state["status"],summary=summary,
                       note="All scheduled jobs validated. Remaining precision, pressure/model questions are explicitly retained, not declared solved."))
            return 0
        except Exception as exc:
            self.state.update(status="BUDGET_EXHAUSTED" if self.budget_stopped else "STOPPED" if self.stopping else "NEEDS_ATTENTION",error=str(exc))
            print(f"Precision campaign stopped: {exc}",file=sys.stderr,flush=True)
            self.update(force=True)
            return 130 if self.stopping else 1
        finally:
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
                try:self.child.wait(timeout=60)
                except subprocess.TimeoutExpired:subprocess.run(["systemctl","--user","--no-block","stop",UNIT],check=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("command",choices=("run","status","report"));args=parser.parse_args()
    if args.command=="status":
        unit=subprocess.run(["systemctl","--user","show",UNIT,"-p","ActiveState","-p","MemoryCurrent","-p","MemorySwapCurrent"],capture_output=True,text=True)
        print(unit.stdout.strip())
        if (RUN/"status.json").exists():
            state=json.loads((RUN/"status.json").read_text());print(json.dumps(state,indent=2))
            if state["status"]=="RUNNING" and ("ActiveState=active" not in unit.stdout or time.time()-datetime.fromisoformat(state["updated_at"]).timestamp()>600):
                print("WARNING: inactive service or stale heartbeat; inspect logs")
        for name in ("timing","refinement"):
            db=RUN/name/"jobs.sqlite"
            if db.exists():
                with sqlite3.connect(db.as_uri()+"?mode=ro",uri=True) as c:print(name,c.execute("SELECT stage,status,COUNT(*) FROM jobs GROUP BY stage,status").fetchall())
        print("Report:",RUN/"report/index.html");return 0
    if args.command=="report":analysis.publish(RUN,BASELINE,json.loads((RUN/"status.json").read_text()));return 0
    RUN.mkdir(parents=True,exist_ok=True)
    with (RUN/"supervisor.lock").open("w") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return PrecisionCampaign(RUN).execute()


if __name__=="__main__":raise SystemExit(main())
