#!/usr/bin/env python3
"""M3R2: fixed-cohort continuation after the reviewed noreset display gate."""
import argparse
from contextlib import contextmanager
import copy
import fcntl
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import xvfb_reset_gate as gate
from scripts.staged_scan import write_json

r, p = gate.retry, gate.p
OLD_RUN = r.RUN
RUN = ROOT/'runs/noble_cf4_mixtures_1atm_noreset'
UNIT = 'srim-mixtures-1atm-noreset.service'
original_audit = p.audit_case


@contextmanager
def historical_audits():
    saved = p.audit_case; p.audit_case = original_audit
    try:
        yield
    finally:
        p.audit_case = saved


def review_gate():
    with historical_audits():
        plan, inputs = gate.prepare(gate.RUN)
        completion = json.loads((gate.RUN/'completion.json').read_text())
        state = json.loads((gate.RUN/'status.json').read_text())
        if {k: v for k, v in completion.items() if k != 'artifact_sha256'} != state:
            raise ValueError('Gate status/completion mismatch')
        if (state['status'] != 'REVIEW_REQUIRED' or not state['source_unchanged']
                or state['diagnostic_histories'] != 100 or state['new_independent_production_histories'] != 0
                or set(state['replays_passed']) != {gate.REFERENCE, gate.FAILED}
                or not 0 < state['wall_seconds'] <= plan['budget_seconds']):
            raise ValueError('Display gate incomplete or out of budget')
        gate.verify_hashes(plan['input_sha256']); gate.verify_hashes(completion['artifact_sha256'])
        for label in ('control', 'noreset'):
            checks = json.loads((gate.RUN/(label+'.json')).read_text())
            if checks['status'] != 'COMPLETE' or checks['completed'] != 60 or len(checks['checks']) != 60:
                raise ValueError('Display checks incomplete')
            for wave in range(4):
                rows = [x for x in checks['checks'] if x['wave'] == wave]
                if len(rows) != 15 or len({x['display'] for x in rows}) != 15 or {x['worker'] for x in rows} != set(range(15)):
                    raise ValueError('Display allocation/accounting changed')
                for row in rows:
                    if (row['status'] != 'COMPLETE' or row['reconnects'] != 20 or not row['synchronized']
                            or not row['authenticated'] or row['tcp_listening'] or not row['server_reaped']
                            or not row['unauthorized_client_rejected']):
                        raise ValueError('Display lifecycle gate failed')
                    if label == 'noreset' and row.get('reset_on_last_client') is not False:
                        raise ValueError('Noreset policy missing')
        if state['reset_hypothesis_reproduced']:
            raise ValueError('Unexpected reset conclusion')
        replays = []
        for case in plan['cases']:
            row = json.loads((gate.RUN/'cases'/case['name']/'result.json').read_text())
            gate.audit_replay(case, row, inputs[case['name']]); replays.append(row)
        _, baseline, *_ = p.previous.review()
    accepted = [row for row in json.loads((OLD_RUN/'results.json').read_text()) if row['status'] == 'COMPLETE']
    if len(accepted) != 401 or len(baseline) != 1044:
        raise ValueError('Production accounting changed')
    summary = dict(status='PASSED_NO_RESET_GATE_NOT_CAUSAL_PROOF', complete_displays_per_launcher=60,
                   reconnects_per_display=20, exact_reference_replay_passed=True, failed_input_replay_passed=True,
                   diagnostic_histories_excluded=100, reset_hypothesis_reproduced=False,
                   gate_wall_seconds=state['wall_seconds'], gate_resources=state['resources'],
                   inherited_production_seconds=plan['inherited_used_seconds'],
                   inherited_used_seconds=plan['inherited_used_seconds']+state['wall_seconds'],
                   retained_production_batches=401, remaining_original_batches=859,
                   decision='Use validated authenticated -noreset launcher without weakening completion or physics checks. '
                            'Both control and new launcher passed; reset causality remains unproved. Preserve original failures, '
                            '401 production results and all frozen inputs/seeds. Exclude both diagnostic replays from production. '
                            'Rerun859 outstanding original batches; carry production plus diagnostic wall time into original24h cap. '
                            'Any new failure holds production; completion requires manual review before P4.')
    return plan, inputs, baseline, accepted, summary


def prepare(run):
    gate_plan, inputs, baseline, accepted, review = review_gate()
    frozen = json.loads((OLD_RUN/'plan.json').read_text())
    names = {row['name'] for row in accepted}
    pending = [case for case in frozen['cases'] if case['name'] not in names]
    estimate = p.forecast(pending)
    estimate['planning_including_spent_hours'] = estimate['planning_hours']+review['inherited_used_seconds']/3600
    if len(pending) != 859 or estimate['planning_including_spent_hours']+(p.JOB_TIMEOUT+90)/3600 > 24:
        raise ValueError('Continuation does not fit inherited budget and drain reserve')
    complete = json.loads((gate.RUN/'completion.json').read_text())
    paths = {Path(__file__), ROOT/'scripts/mixtures_1atm_noreset.sh', gate.RUN/'plan.json',
             gate.RUN/'completion.json', gate.RUN/'status.json'}
    manifest = {**gate_plan['input_sha256'], **complete['artifact_sha256'],
                **{str(path): p.history.actual_sha256(path) for path in sorted(paths)}}
    plan = copy.deepcopy(frozen)
    plan.update(stage='M3R2', previous_failed_run=str(OLD_RUN), display_gate=str(gate.RUN),
                retained_original_cases=sorted(names), new_cases_to_run=859,
                inherited_used_seconds=review['inherited_used_seconds'], failure_review=review,
                display_policy='authenticated-displayfd-noreset-v1', forecast=estimate, input_sha256=manifest)
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen M3R2 continuation changed')
    if not path.exists():
        write_json(path, plan)
    write_json(run/'previous_review.json', review)
    for row in accepted:
        revised = dict(row, retained_from=str(OLD_RUN/'cases'/row['name']/'result.json'))
        path = run/'cases'/row['name']/'result.json'
        if path.exists() and json.loads(path.read_text()) != revised:
            raise ValueError('Retained production record changed')
        if not path.exists():
            write_json(path, revised)
    budget = run/'budget.json'
    if not budget.exists():
        write_json(budget, dict(used_seconds=review['inherited_used_seconds'], limit_seconds=p.BUDGET))
    else:
        saved = json.loads(budget.read_text())
        if saved['used_seconds'] < review['inherited_used_seconds'] or saved['limit_seconds'] != p.BUDGET:
            raise ValueError('Inherited production/diagnostic budget reset')
    return plan, inputs, baseline


def audit_case(case, row, contents):
    original_audit(case, row, contents)
    if 'retained_from' in row:
        expected = OLD_RUN/'cases'/case['name']/'result.json'
        if row['retained_from'] != str(expected) or {k: v for k, v in row.items() if k != 'retained_from'} != json.loads(expected.read_text()):
            raise ValueError('Retained row does not match frozen production')
    elif (not Path(row['attempt']).is_relative_to(RUN/'cases') or
          row['display_startup'].get('reset_on_last_client') is not False or
          row['display_startup'].get('helper') != str(Path(gate.fixed_display.__file__))):
        raise ValueError('New production missing validated noreset display evidence')


def publish(state, rows, retained):
    state.update(stage='M3R2', display_policy='authenticated-displayfd-noreset-v1',
                 retained_original_batches=sum('retained_from' in row for row in rows),
                 completed_continuation_batches=sum(row['status'] == 'COMPLETE' and 'retained_from' not in row for row in rows),
                 diagnostic_histories_excluded=100, reset_causality_proven=False)
    r.original_publish(state, rows, retained)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(RUN)
            print(json.dumps({k: plan[k] for k in ('stage', 'new_cases_to_run', 'inherited_used_seconds', 'forecast')}, indent=2)); return 0
        p.RUN, p.UNIT, p.prepare, p.publish = RUN, UNIT, prepare, publish
        p.audit_case, p.check_composition = audit_case, r.check_composition
        p.b.RUN, p.b.base.RUN, p.scan.launch_command = RUN, RUN, gate.launch
        return p.execute()


if __name__ == '__main__':
    raise SystemExit(main())
