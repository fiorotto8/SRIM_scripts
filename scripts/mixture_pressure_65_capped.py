#!/usr/bin/env python3
"""G5-65R2: production only, unchanged cohort, lower aggregate CPU duty."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixture_pressure_65_isolated as r
from scripts import thermal_95_gate as thermal
from scripts.staged_scan import write_json

RUN = ROOT/'runs/noble_cf4_mixture_pressure_65_capped'
UNIT = 'srim-mixture-pressure-65-capped.service'
PRIOR = ROOT/'runs/noble_cf4_mixture_pressure_65_isolated'
LAST = ROOT/'runs/thermal_95_gate_retry'
CPU_MAX = '200000 20000'  # 1000%, 20ms period; still at most15 worker processes.
original_publish, original_launch = r.publish, r.launch_case


def read(root, name):
    return json.loads((root/name).read_text())


def review_stop():
    plan = read(LAST, 'plan.json')
    r.old.gate.verify_hashes(plan['input_sha256'])
    state, guard, budget, exited = (read(LAST, n) for n in
        ('status.json', 'thermal-status.json', 'budget.json', 'service-exit.json'))
    if (state['status'] != 'NEEDS_ATTENTION' or state['completed_replays'] != 0
            or guard['error'] != 'Thermal guard: CPU throttle counter changed'
            or guard['status'] != 'NEEDS_ATTENTION' or (LAST/'completion.json').exists()
            or list((LAST/'cases').glob('*/result.json'))
            or [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['exit-code', 'exited', '1']):
        raise ValueError('Previous thermal stop changed; review required')
    baseline = guard['baseline']; thermal.thermal_check(baseline, baseline, initial=True)
    samples = [json.loads(line) for line in (LAST/'thermal-samples.jsonl').read_text().splitlines()]
    for sample in samples:
        thermal.thermal_check(sample, baseline)
    try:
        thermal.thermal_check(guard['latest'], baseline)
    except RuntimeError as exc:
        if str(exc) != guard['error']:
            raise
    else:
        raise ValueError('Missing recorded thermal trigger')
    if len(samples)+1 != guard['samples']:
        raise ValueError('Incomplete previous thermal trace')
    elapsed = (datetime.fromisoformat(exited['ended_at'])-datetime.fromisoformat(guard['started_at'])).total_seconds()
    if budget['limit_seconds'] != r.BUDGET or budget['used_seconds'] < plan['inherited_used_seconds']+elapsed-.001:
        raise ValueError('Previous elapsed budget lost')
    paths = {LAST/n for n in ('plan.json', 'status.json', 'thermal-status.json', 'thermal-samples.jsonl',
                             'budget.json', 'service-exit.json', 'session.json', 'logs/campaign.log')}
    paths.update(x for x in (LAST/'cases').rglob('*') if x.is_file())
    for runtime in (LAST/'runtime').glob('worker-*'):
        paths.update(r.scan.collect_runtime_outputs(runtime).values())
        paths.update(x for x in runtime.iterdir() if x.is_file() and
                     (x.name.upper() in ('TRIM.IN', 'TRIM.DAT') or x.name.startswith('.')))
    manifest = dict(plan['input_sha256'])
    manifest.update({str(x): r.p.history.actual_sha256(x) for x in sorted(paths)})
    return budget['used_seconds'], manifest


def prepare():
    frozen, inputs, baseline, accepted, _, manifest = r.review.review()
    previous = read(PRIOR, 'plan.json'); r.old.gate.verify_hashes(previous['input_sha256'])
    if read(PRIOR, 'results.json') != accepted:
        raise ValueError('Retained production changed')
    used, stopped_manifest = review_stop()
    names = sorted(row['name'] for row in accepted)
    pending = [c for c in frozen['cases'] if c['name'] not in names]
    if len(accepted) != 251 or len(baseline) != 4536 or len(pending) != 1009:
        raise ValueError('Original production accounting changed')
    forecast = r.old.forecast([dict(c, estimated_seconds=c['estimated_seconds']*1.5) for c in pending])
    forecast['planning_hours'] += len(pending)/3600  # Conservative allowance for1s launch stagger.
    forecast['method'] += ' CPU-duty extrapolation15/10, not measured at reduced quota; plus1s per pending launch.'
    if used/3600+forecast['planning_hours']+(r.JOB_TIMEOUT+90)/3600 > 24:
        raise ValueError('Original cumulative budget insufficient')
    manifest = {**manifest, **previous['input_sha256'], **stopped_manifest}
    for case in pending:
        for name, content, digest in zip(('TRIM.IN', 'TRIM.DAT'), inputs[case['name']],
                                        (case['input_sha256'], case['trim_dat_sha256'])):
            path = RUN/'inputs'/case['name']/name
            if hashlib.sha256(content).hexdigest() != digest or (path.exists() and path.read_bytes() != content):
                raise ValueError('Original production input changed')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            manifest[str(path)] = digest
    for path in (Path(__file__), ROOT/'scripts/mixture_pressure_65_capped.sh', Path(thermal.__file__),
                 ROOT/'scripts/thermal_95_gate_retry.py'):
        manifest[str(path)] = r.p.history.actual_sha256(path)
    plan = dict(previous, stage='G5-65R2', input_sha256=manifest, inherited_used_seconds=used,
                forecast=forecast, previous_failed_run=str(LAST), retained_original_names=names,
                thermal_policy=dict(read(LAST, 'plan.json')['thermal_policy']),
                cpu_quota_percent=1000, cpu_quota_period_usec=20000, cpu_max=CPU_MAX,
                thread_environment=thermal.THREAD_ENV, startup_stagger_seconds=1,
                diagnostic_histories_scheduled=0,
                user_direction='Skip further load tests; continue actual scans. C1h canceled, not passed. '
                               'Scientific acceptance gates unchanged; no claim of validated sustained-load reliability.')
    if (RUN/'plan.json').exists() and read(RUN, 'plan.json') != plan:
        raise ValueError('Frozen production continuation changed')
    if not (RUN/'plan.json').exists():
        write_json(RUN/'plan.json', plan)
    if not (RUN/'budget.json').exists():
        write_json(RUN/'budget.json', dict(used_seconds=used, limit_seconds=r.BUDGET))
    budget = read(RUN, 'budget.json')
    if budget['used_seconds'] < used or budget['limit_seconds'] != r.BUDGET:
        raise ValueError('Inherited budget reset')
    return plan, inputs, baseline, accepted


def publish(state, rows, baseline):
    state.update(stage='G5-65R2', cpu_quota_percent=1000, worker_process_limit=15,
                 load_test_skipped_by_user=True)
    if state.get('status') == 'REVIEW_REQUIRED':
        state['phase'] = 'G5-65R2 complete; review before G5-70'
    original_publish(state, rows, baseline)


def launch_case(case, runtime, deadline):
    time.sleep(1)
    return original_launch(case, runtime, deadline)


def guard():
    group = Path(r.resource_sample()['cgroup'])
    if group.name != UNIT or (group/'cpu.max').read_text().strip() != CPU_MAX:
        raise RuntimeError('Production requires exact1000% CPU quota with20ms period')
    budget = read(RUN, 'budget.json')
    if budget != dict(used_seconds=read(RUN, 'plan.json')['inherited_used_seconds'], limit_seconds=r.BUDGET):
        raise ValueError('No automatic restart or reset of cumulative budget')
    return thermal.guard()


def bind():
    r.RUN, r.UNIT, r.__file__ = RUN, UNIT, __file__
    r.prepare, r.publish, r.launch_case, r.guard = prepare, publish, launch_case, guard
    thermal.RUN, thermal.UNIT, thermal.__file__, thermal.BUDGET = RUN, UNIT, __file__, r.BUDGET


if __name__ == '__main__':
    bind()
    raise SystemExit(r.main())
