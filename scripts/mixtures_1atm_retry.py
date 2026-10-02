#!/usr/bin/env python3
"""Versioned M3 continuation: omitted decimal zeros, unchanged physical cohort."""
import argparse
from contextlib import contextmanager
import copy
from decimal import Decimal
import fcntl
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import mixtures_1atm as p
from scripts.staged_scan import write_json

OLD_RUN = ROOT/'runs/noble_cf4_mixtures_1atm'
RUN = ROOT/'runs/noble_cf4_mixtures_1atm_retry'
UNIT = 'srim-mixtures-1atm-retry.service'
original_prepare, original_publish = p.prepare, p.publish
original_composition = p.check_composition
PARSER = 'srim-percent-omitted-decimal-zero-v2'
FAILED = {'Ar_Ar_e1_n75_p1_b0', 'Xe_Xe_e1_n75_p1_b0'}


@contextmanager
def frozen_seed_context():
    """Reconstruct the M3 predecessor registry, excluding later dependent plans."""
    frozen = json.loads((OLD_RUN/'plan.json').read_text()); seeds = set(); paths = []
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('seed', 'random_seed') and isinstance(child, int) and child > 0:
                    seeds.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    for name, digest in sorted(frozen['input_sha256'].items()):
        path = Path(name)
        if path.name == 'plan.json' and path.parent.parent == ROOT/'runs':
            if p.history.actual_sha256(path) != digest:
                raise ValueError('Frozen predecessor plan changed')
            visit(json.loads(path.read_text())); paths.append(path)
    if (len(seeds) != frozen['forbidden_previous_seed_count'] or
            hashlib.sha256(json.dumps(sorted(seeds)).encode()).hexdigest() != frozen['forbidden_previous_seeds_sha256']):
        raise ValueError('Frozen predecessor seed registry changed')
    saved = p.known_seeds
    def registry(rows):
        if not {r['seed'] for r in rows} <= seeds:
            raise ValueError('Unexpected seed in historical baseline')
        return set(seeds), list(paths)
    p.known_seeds = registry
    try:
        yield
    finally:
        p.known_seeds = saved


def check_composition(case, outputs):
    text = p.scan.find_output(outputs, 'TDATA.TXT').read_text(encoding='latin1')
    matches = re.findall(r'Layer #\s*1-\s*(\w+)\s*=\s*([\d.Ee+\-]+)\s+Atomic Percent\s*=\s*([\d.Ee+\-]+)\s+Mass Percent', text)
    if len(matches) != 3 or {m[0] for m in matches} != {'C', 'F', case['family']}:
        raise ValueError('Actual target composition species mismatch')
    reported = {}
    for symbol, atom, mass in matches:
        entry = {}
        for field, raw in [('atomic', atom), ('mass', mass)]:
            value = float(raw); quantum = float(Decimal(10)**Decimal(raw).as_tuple().exponent)
            # SRIM prints "50" for 50.0. Never widen the prior 0.1 percentage-
            # point ceiling to one percentage point just because zeros vanished.
            margin = min(quantum, .1)
            expected = 100*case[field+'_fractions'][symbol]
            if not math.isfinite(value) or value <= 0 or abs(value-expected) > margin+1e-10:
                raise ValueError('Actual target '+field+' fraction mismatch')
            entry[field+'_percent'] = value; entry[field+'_resolution_percent'] = quantum
            entry[field+'_acceptance_margin_percent'] = margin
        reported[symbol] = entry
    if not re.search(r'Layer #\s*1\s*-\s*'+re.escape(case['condition_id'])+r'\s*$', text, re.MULTILINE):
        raise ValueError('Actual gas layer identity mismatch')
    return dict(reported_composition=reported, composition_parameters_match=True, composition_parser=PARSER)


@contextmanager
def parser_context():
    saved = p.check_composition; p.check_composition = check_composition
    try:
        yield
    finally:
        p.check_composition = saved


def review_failed():
    with frozen_seed_context():
        plan, inputs, baseline = original_prepare(OLD_RUN)
    state = json.loads((OLD_RUN/'status.json').read_text()); rows = json.loads((OLD_RUN/'results.json').read_text())
    cases = {c['name']: c for c in plan['cases']}
    if state['status'] != 'NEEDS_ATTENTION' or len(rows) != 17 or len({r['name'] for r in rows}) != 17:
        raise ValueError('Unexpected original M3 failure state/count')
    failed = [r for r in rows if r['status'] == 'FAILED']
    if {r['name'] for r in failed} != FAILED or any(r['error'] != 'Actual target atomic fraction mismatch' for r in failed):
        raise ValueError('Not the reviewed integer-header validation failure')
    accepted = []
    for row in rows:
        case = cases[row['name']]; directory = Path(row['attempt'])
        if row != json.loads((OLD_RUN/'cases'/row['name']/'result.json').read_text()) or any(row[k] != v for k, v in case.items()):
            raise ValueError('Original case/index changed')
        if any(p.history.actual_sha256(path) != digest for path, digest in row['artifact_sha256'].items()):
            raise ValueError('Original raw artifact changed')
        if (directory/'TRIM.IN').read_bytes() != inputs[row['name']][0] or (directory/'TRIM.DAT').read_bytes() != inputs[row['name']][1]:
            raise ValueError('Original executed input changed')
        if row['status'] == 'STOPPED':
            continue
        outputs = {path.name.upper(): path for path in directory.iterdir()}
        if row['status'] == 'COMPLETE':
            prior_fields = original_composition(case, outputs)
            if any(row[k] != v for k, v in prior_fields.items()):
                raise ValueError('Original composition analysis changed')
        elif (case['noble_percent'] != 75 or not row.get('normal_completion_acknowledged') or
              not re.search(r'F\s*=\s*50\s+Atomic Percent', (directory/'TDATA.TXT').read_text(encoding='latin1'))):
            raise ValueError('Unexpected failed-header lifecycle')
        revised = copy.deepcopy(row); revised['status'] = 'COMPLETE'
        if 'error' in revised:
            revised['previous_validation_error'] = revised.pop('error')
        revised.update(check_composition(case, outputs), reused_from=str(OLD_RUN/'cases'/row['name']/'result.json'))
        with parser_context():
            p.audit_case(case, revised, inputs[row['name']])
        accepted.append(revised)
    if len(accepted) != 10 or sum(r['status'] == 'STOPPED' for r in rows) != 7:
        raise ValueError('Unexpected recoverable batch accounting')
    spent = json.loads((OLD_RUN/'budget.json').read_text())['used_seconds']
    summary = dict(status='AUDITED_INTEGER_PERCENT_HEADER_PRESENTATION_FAILURE', original_cases=17,
                   previously_complete=8, completed_raw_batches_revalidated=2, accepted_independent_batches=10,
                   stopped_incomplete_cases=7, unlaunched_cases=1243, remaining_cases=1250,
                   inherited_used_seconds=spent, original_resources=state['resources'],
                   cause='Expected F atomic fraction50% is printed as50 rather than50.0 at75% noble. '
                         'Original parser rejected the inferred integer resolution despite zero numeric discrepancy. '
                         'Both batches passed exact inputs, density/bindings/species/energy,50 contained endpoints and normal completion.',
                   correction='Versioned parser accepts omitted decimal zeros, retaining the0.1-percentage-point '
                              'maximum acceptance margin (never1 point). Fine printed decimals keep their tighter margin. '
                              'Original failed records, plan and source remain untouched.',
                   decision='Retain ten fully raw-audited independent production batches exactly once. '
                            'Rerun only seven interrupted inputs and1243 unlaunched cases with unchanged frozen seeds/model. '
                            'Remaining35 low-energy b0 cases gate1215 other batches. Carry spent time into original24h cap; '
                            'no automatic P4, added samples or precision topups.')
    return plan, inputs, baseline, accepted, summary


def prepare(run):
    frozen, inputs, baseline, accepted, review = review_failed()
    names = {r['name'] for r in accepted}
    estimate = p.forecast([c for c in frozen['cases'] if c['name'] not in names])
    estimate['planning_including_spent_hours'] = estimate['planning_hours']+review['inherited_used_seconds']/3600
    if estimate['planning_including_spent_hours'] > 21:
        raise ValueError('Continuation does not fit original budget with drain reserve')
    paths = {Path(__file__), ROOT/'scripts/mixtures_1atm_retry.sh', OLD_RUN/'plan.json', OLD_RUN/'status.json',
             OLD_RUN/'results.json', OLD_RUN/'budget.json', OLD_RUN/'release-seed-audit.json'}
    for row in json.loads((OLD_RUN/'results.json').read_text()):
        paths.add(OLD_RUN/'cases'/row['name']/'result.json'); paths.update(Path(path) for path in row['artifact_sha256'])
    plan = copy.deepcopy(frozen)
    plan.update(stage='M3R', previous_failed_run=str(OLD_RUN), retained_original_cases=sorted(names), new_cases_to_run=1250,
                header_parser_revision=PARSER, failure_review=review, inherited_used_seconds=review['inherited_used_seconds'], forecast=estimate,
                input_sha256={**frozen['input_sha256'], **{str(path): p.history.actual_sha256(path) for path in sorted(paths)}})
    path = run/'plan.json'
    if path.exists() and json.loads(path.read_text()) != plan:
        raise ValueError('Frozen M3 continuation changed')
    if not path.exists():
        write_json(path, plan)
    write_json(run/'previous_review.json', review)
    for row in accepted:
        path = run/'cases'/row['name']/'result.json'
        if path.exists() and json.loads(path.read_text()) != row:
            raise ValueError('Retained M3 raw audit changed')
        if not path.exists():
            write_json(path, row)
    budget = run/'budget.json'
    if not budget.exists():
        write_json(budget, dict(used_seconds=review['inherited_used_seconds'], limit_seconds=p.BUDGET))
    elif json.loads(budget.read_text())['used_seconds'] < review['inherited_used_seconds']:
        raise ValueError('Continuation reset prior elapsed budget')
    return plan, inputs, baseline


def publish(state, rows, retained):
    state.update(stage='M3R', previous_failed_run=str(OLD_RUN), composition_parser=PARSER,
                 retained_original_batches=sum('reused_from' in r for r in rows),
                 completed_continuation_batches=sum(r['status'] == 'COMPLETE' and 'reused_from' not in r for r in rows))
    original_publish(state, rows, retained)


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=('prepare', 'run', 'status')); args = parser.parse_args()
    if args.command == 'status':
        subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MemoryCurrent', '-p', 'MemorySwapCurrent'])
        if (RUN/'status.json').exists():
            print((RUN/'status.json').read_text())
        print('Report:', RUN/'index.html'); return 0
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'prepare':
            plan, *_ = prepare(RUN); print(json.dumps({k: plan[k] for k in ('stage', 'new_cases_to_run', 'inherited_used_seconds', 'forecast')}, indent=2)); return 0
        p.RUN, p.UNIT, p.prepare, p.publish, p.check_composition = RUN, UNIT, prepare, publish, check_composition
        p.b.RUN, p.b.base.RUN, p.scan.launch_command = RUN, RUN, p.prior.r.launch
        return p.execute()


if __name__ == '__main__':
    raise SystemExit(main())
