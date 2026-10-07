#!/usr/bin/env python3
"""G5-65R3: original production cohort; throttling warnings, temperature hard stop."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixture_pressure_65_capped as c

r, thermal = c.r, c.thermal
RUN = ROOT/'runs/noble_cf4_mixture_pressure_65_temperature'
PRIOR = ROOT/'runs/noble_cf4_mixture_pressure_65_capped'
UNIT = 'srim-mixture-pressure-65-temperature.service'
strict_check = thermal.thermal_check


def temperature_check(sample, baseline, initial=False):
    """Ignore only increasing throttle counts; keep every other telemetry gate."""
    counts, initial_counts = sample['throttle_counts'], baseline['throttle_counts']
    if counts.keys() != initial_counts.keys():
        raise RuntimeError('Thermal telemetry set changed')
    if any(not isinstance(v, int) or v < initial_counts[k] for k, v in counts.items()):
        raise RuntimeError('Thermal throttle counter invalid or reset')
    strict_check(dict(sample, throttle_counts=initial_counts), baseline, initial=initial)


def review_stop(accepted):
    plan = c.read(PRIOR, 'plan.json'); r.old.gate.verify_hashes(plan['input_sha256'])
    state, guard, budget, exited = (c.read(PRIOR, n) for n in
        ('status.json', 'thermal-status.json', 'budget.json', 'service-exit.json'))
    if (state['status'] != 'NEEDS_ATTENTION' or state['completed_continuation_batches'] != 0
            or guard['status'] != 'NEEDS_ATTENTION'
            or guard['error'] != 'Thermal guard: CPU throttle counter changed'
            or (PRIOR/'completion.json').exists() or list((PRIOR/'cases').glob('*/result.json'))
            or c.read(PRIOR, 'results.json') != accepted
            or [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['exit-code', 'exited', '1']):
        raise ValueError('Previous production stop changed')
    samples = [json.loads(line) for line in (PRIOR/'thermal-samples.jsonl').read_text().splitlines()]
    strict_check(guard['baseline'], guard['baseline'], initial=True)
    for sample in samples:
        strict_check(sample, guard['baseline'])
    try:
        strict_check(guard['latest'], guard['baseline'])
    except RuntimeError as exc:
        if str(exc) != guard['error']:
            raise
    else:
        raise ValueError('Missing previous throttle trigger')
    if len(samples)+1 != guard['samples']:
        raise ValueError('Incomplete previous thermal trace')
    elapsed = (datetime.fromisoformat(exited['ended_at'])-datetime.fromisoformat(guard['started_at'])).total_seconds()
    if budget['limit_seconds'] != r.BUDGET or budget['used_seconds'] < plan['inherited_used_seconds']+elapsed-.001:
        raise ValueError('Inherited production budget lost')
    paths = {PRIOR/n for n in ('plan.json', 'status.json', 'results.json', 'thermal-status.json',
                              'thermal-samples.jsonl', 'budget.json', 'service-exit.json', 'session.json', 'logs/campaign.log')}
    paths.update(x for x in (PRIOR/'cases').rglob('*') if x.is_file())
    for runtime in (PRIOR/'runtime').glob('worker-*'):
        paths.update(r.scan.collect_runtime_outputs(runtime).values())
        paths.update(x for x in runtime.iterdir() if x.is_file() and
                     (x.name.upper() in ('TRIM.IN', 'TRIM.DAT') or x.name.startswith('.')))
    manifest = dict(plan['input_sha256'])
    manifest.update({str(x): r.p.history.actual_sha256(x) for x in sorted(paths)})
    return plan, budget['used_seconds'], manifest


def prepare():
    frozen, inputs, baseline, accepted, _, _ = r.review.review()
    previous, used, manifest = review_stop(accepted)
    names = sorted(row['name'] for row in accepted)
    pending = [case for case in frozen['cases'] if case['name'] not in names]
    if (len(accepted), len(baseline), len(pending)) != (251, 4536, 1009) or frozen['cases'] != previous['cases']:
        raise ValueError('Original cohort changed')
    if used/3600+previous['forecast']['planning_hours']+(r.JOB_TIMEOUT+90)/3600 > 24:
        raise ValueError('Insufficient original cumulative budget')
    for case in pending:
        for name, content, digest in zip(('TRIM.IN', 'TRIM.DAT'), inputs[case['name']],
                                        (case['input_sha256'], case['trim_dat_sha256'])):
            path = RUN/'inputs'/case['name']/name
            if hashlib.sha256(content).hexdigest() != digest or (path.exists() and path.read_bytes() != content):
                raise ValueError('Frozen input changed')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            manifest[str(path)] = digest
    for path in (Path(__file__), ROOT/'scripts/mixture_pressure_65_temperature.sh'):
        manifest[str(path)] = r.p.history.actual_sha256(path)
    policy = dict(previous['thermal_policy'], stop_on_new_throttling=False,
                  warn_on_new_throttling=True, stop_on_counter_reset=True)
    plan = dict(previous, stage='G5-65R3', input_sha256=manifest, inherited_used_seconds=used,
                previous_failed_run=str(PRIOR), thermal_policy=policy,
                user_direction='User approved throttle-counter warnings instead of stops, retaining95C cutoff, '
                               'telemetry/resource/scientific gates and hardware protections. Production only; no load test.')
    if (RUN/'plan.json').exists() and c.read(RUN, 'plan.json') != plan:
        raise ValueError('Frozen continuation changed')
    if not (RUN/'plan.json').exists():
        c.write_json(RUN/'plan.json', plan)
    if not (RUN/'budget.json').exists():
        c.write_json(RUN/'budget.json', dict(used_seconds=used, limit_seconds=r.BUDGET))
    budget = c.read(RUN, 'budget.json')
    if budget['used_seconds'] < used or budget['limit_seconds'] != r.BUDGET:
        raise ValueError('Inherited budget reset')
    return plan, inputs, baseline, accepted


def publish(state, rows, baseline):
    state.update(stage='G5-65R3', cpu_quota_percent=1000, worker_process_limit=15,
                 load_test_skipped_by_user=True, stop_on_new_throttling=False,
                 warn_on_new_throttling=True, stop_threshold_c=95)
    if state.get('status') == 'REVIEW_REQUIRED':
        state['phase'] = 'G5-65R3 complete; review before G5-70'
    c.original_publish(state, rows, baseline)


def guard():
    last_counts = None
    def check(sample, baseline, initial=False):
        nonlocal last_counts
        temperature_check(sample, baseline, initial=initial)
        counts = sample['throttle_counts']
        previous = last_counts if last_counts is not None else baseline['throttle_counts']
        if any(counts[k] < previous[k] for k in counts):
            raise RuntimeError('Thermal throttle counter reset between samples')
        delta = {k: counts[k]-v for k, v in previous.items() if counts[k] != v}
        sample['throttle_warning'] = dict(counter_increases_since_start={
            k: counts[k]-v for k, v in baseline['throttle_counts'].items() if counts[k] != v},
            policy='warning only;95C temperature stop remains active')
        if delta:
            event = dict(time=sample['time'], warning='CPU thermal throttling; continuing below temperature stop',
                         counter_deltas=delta, sampled_max_c=max(sample['temperatures_c'].values()))
            with (RUN/'throttle-warnings.jsonl').open('a') as stream:
                stream.write(json.dumps(event)+'\n')
            print(json.dumps(event), flush=True)
        last_counts = dict(counts)
    saved = thermal.thermal_check
    thermal.thermal_check = check
    try:
        return c.guard()
    finally:
        thermal.thermal_check = saved


def bind():
    c.RUN, c.UNIT = RUN, UNIT
    c.bind()
    r.__file__ = thermal.__file__ = __file__
    r.prepare, r.publish, r.guard = prepare, publish, guard


if __name__ == '__main__':
    bind()
    raise SystemExit(r.main())
