#!/usr/bin/env python3
"""Cold audit of C1f, including exact replays and the complete thermal trace."""
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import supervisor_crash_gate as g
from scripts import crash_gate_thermal_guard as thermal


def read(name):
    return json.loads((g.RUN/name).read_text())


def validate_thermal(state, samples):
    if (state['status'] != 'COMPLETE' or state['child_returncode'] != 0
            or len(samples) != state['samples'] or not samples or state['latest'] != samples[-1]):
        raise ValueError('Thermal completion/accounting mismatch')
    baseline = state['baseline']; thermal.check(baseline, baseline, initial=True)
    times = [datetime.fromisoformat(baseline['time']).timestamp()]
    peak = max(baseline['temperatures_c'].values())
    for sample in samples:
        thermal.check(sample, baseline)
        times.append(datetime.fromisoformat(sample['time']).timestamp())
        peak = max(peak, max(sample['temperatures_c'].values()))
    if peak != state['peak_temperature_c'] or any(not 0 < b-a < 2 for a, b in zip(times, times[1:])):
        raise ValueError('Thermal peak or continuous trace changed')
    start = datetime.fromisoformat(state['started_at']).timestamp()
    end = datetime.fromisoformat(state['ended_at']).timestamp()
    if not 0 <= times[0]-start < 2 or not 0 <= end-times[-1] < 2 or not 0 < end-start <= g.BUDGET:
        raise ValueError('Thermal trace does not cover the bounded run')
    return end-start


def review():
    frozen, inputs, baseline = g.old.prepare(g.old.RUN)
    plan = read('plan.json'); state = read('status.json')
    g.gate.verify_hashes(plan['input_sha256'])
    tplan = thermal.prepare(); g.gate.verify_hashes(tplan['input_sha256'])
    if (state != read('completion.json') or state['status'] != 'REVIEW_REQUIRED'
            or not state['source_unchanged'] or state['exact_replays_passed'] != 15
            or state['threaded_batch_audits'] != 753 or state['diagnostic_histories'] != 750
            or state['new_independent_production_histories'] != 0
            or state['root_cause_proven'] or state['spontaneous_sigsegv_reproduced']
            or not 0 < state['wall_seconds'] <= g.BUDGET or state['resources']['memory_swap_current_gib'] != 0):
        raise ValueError('C1f completion gate failed')
    exited = read('service-exit.json')
    if [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['success', 'exited', '0']:
        raise ValueError('C1f service did not exit cleanly')
    if read('stress.json') != dict(status='COMPLETE', passes=3, batch_audits=753, sigsegv_reproduced=False):
        raise ValueError('Threaded saved-output stress incomplete')
    if read('fault-injection.json') != dict(child_returncode=-11, parent_survived=True, intentional=True):
        raise ValueError('Controlled fault containment failed')
    if 'Fatal Python error: Segmentation fault' not in (g.RUN/'fault-injection.log').read_text():
        raise ValueError('Controlled native fault trace missing')
    samples = [json.loads(line) for line in (g.RUN/'thermal-samples.jsonl').read_text().splitlines()]
    duration = validate_thermal(read('thermal-status.json'), samples)
    accepted = json.loads((g.old.RUN/'results.json').read_text())
    indexed = {c['name']: c for c in frozen['cases']}
    if len(accepted) != 251 or sorted(r['name'] for r in accepted) != plan['accepted_names'] or len(baseline) != 4536:
        raise ValueError('Saved production cohort changed')
    paths = {g.RUN/name for name in ('plan.json', 'status.json', 'completion.json', 'service-exit.json',
             'stress.json', 'stress.log', 'fault-injection.json', 'fault-injection.log', 'thermal-plan.json',
             'thermal-status.json', 'thermal-samples.jsonl', 'results.json', 'logs/campaign.log')}
    replay_rows = read('results.json')
    if len(replay_rows) != 15 or {r['name'] for r in replay_rows} != {c['name'] for c in plan['cases']}:
        raise ValueError('Diagnostic cohort changed')
    with g.gate.retry.parser_context():
        for row in accepted:
            path = g.old.RUN/'cases'/row['name']/'result.json'
            if row != json.loads(path.read_text()) or row['diagnostic']:
                raise ValueError('Saved production/index mismatch')
            g.old.audit_case(indexed[row['name']], row, inputs[row['name']])
        for case in plan['cases']:
            row = next(r for r in replay_rows if r['name'] == case['name'])
            path = g.RUN/'cases'/case['name']/'result.json'
            original = indexed[case['name']]
            reference = next(r['attempt'] for r in accepted if r['name'] == case['name'])
            if (case != dict(original, diagnostic=True, replay_reference=reference)
                    or row != json.loads(path.read_text()) or not row['diagnostic']):
                raise ValueError('Replay input/identity changed')
            g.audit_replay(case, row, inputs[case['name']])
            paths.update(Path(x) for x in row['artifact_sha256'])
            paths.update((path, g.RUN/(case['name']+'.log')))
    if len({r['seed'] for r in baseline+accepted}) != 4787:
        raise ValueError('Accepted production seed independence changed')
    inherited = plan['review']['inherited_used_seconds']+duration
    summary = dict(status='C1F_AUDITED_READY_FOR_GUARDED_CONTINUATION', retained_original_batches=251,
                   remaining_original_batches=1009, exact_replays=15, diagnostic_histories_excluded=750,
                   threaded_batch_audits=753, root_cause_proven=False, diagnostic_wall_seconds=duration,
                   inherited_used_seconds=inherited, thermal_peak_c=read('thermal-status.json')['peak_temperature_c'],
                   thermal_samples=len(samples), new_throttling=False,
                   decision='Short gate passed; not proof of native crash cause or sustained-load stability. '
                            'Keep single-threaded controller and isolated batch processes, faulthandler, '
                            '85C/lower hardware threshold and stop on any new throttling. Preserve original '
                            'inputs, seeds, 251 accepted batches and interrupted artifacts; exclude diagnostics. '
                            'Only1009 outstanding G5-65 batches; manual callback before G5-70. '
                            'Charge full thermal-guarded diagnostic wall time to original24h budget.')
    manifest = {**plan['input_sha256'], **tplan['input_sha256'],
                **{str(x): g.p.history.actual_sha256(x) for x in sorted(paths)}}
    return frozen, inputs, baseline, accepted, summary, manifest


if __name__ == '__main__':
    print(json.dumps(review()[4], indent=2))
