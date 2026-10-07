#!/usr/bin/env python3
"""C1gR: correct detached working directory; preserve zero-simulation C1g failure."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import thermal_95_gate as gate
from scripts.staged_scan import write_json

OLD_RUN, OLD_FILE = gate.RUN, gate.__file__
RUN = ROOT/'runs/thermal_95_gate_retry'
UNIT = 'srim-thermal-95-gate-retry.service'
original_prepare = gate.prepare


def prepare():
    saved_run, saved_file = gate.RUN, gate.__file__
    gate.RUN, gate.__file__ = OLD_RUN, OLD_FILE
    try:
        original, inputs = original_prepare()
    finally:
        gate.RUN, gate.__file__ = saved_run, saved_file
    def read(name):
        return json.loads((OLD_RUN/name).read_text())
    state, thermal, budget, exited = (read(n) for n in ('status.json', 'thermal-status.json', 'budget.json', 'service-exit.json'))
    if (state['status'] != 'NEEDS_ATTENTION' or 'rev-parse' not in state['guard_error']
            or thermal['samples'] != 0 or thermal['stopped_worker_pids'] != []
            or (OLD_RUN/'cases').exists() or (OLD_RUN/'runtime').exists() or (OLD_RUN/'completion.json').exists()
            or [exited[k] for k in ('service_result', 'exit_code', 'exit_status')] != ['exit-code', 'exited', '1']
            or budget['used_seconds'] < original['inherited_used_seconds'] or budget['limit_seconds'] != 86400):
        raise ValueError('Expected preserved zero-simulation working-directory failure')
    if 'fatal: not a git repository' not in (OLD_RUN/'logs/campaign.log').read_text():
        raise ValueError('Working-directory failure evidence missing')
    paths = {Path(__file__), ROOT/'scripts/thermal_95_gate_retry.sh'}
    paths.update(OLD_RUN/n for n in ('plan.json', 'status.json', 'thermal-status.json', 'budget.json', 'service-exit.json', 'logs/campaign.log'))
    manifest = {**original['input_sha256'], **{str(x): gate.p.history.actual_sha256(x) for x in sorted(paths)}}
    plan = dict(original, stage='C1gR', inherited_used_seconds=budget['used_seconds'], input_sha256=manifest,
                startup_recovery=dict(failed_run=str(OLD_RUN), simulations_started=0,
                                      correction='Set systemd WorkingDirectory explicitly to the repository',
                                      working_directory=str(ROOT), previous_failure_seconds=budget['used_seconds']-original['inherited_used_seconds']))
    path = RUN/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen working-directory recovery changed')
    if not path.exists():
        write_json(path, plan)
    return plan, inputs


def bind():
    gate.RUN, gate.UNIT, gate.__file__, gate.prepare = RUN, UNIT, __file__, prepare


if __name__ == '__main__':
    bind()
    raise SystemExit(gate.main())
