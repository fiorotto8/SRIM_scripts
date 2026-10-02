import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from scripts import mixtures_1atm as p
from srim_gas_sweep import load_elements


class MixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (p.environment.RUN/'completion.json').exists():
            raise unittest.SkipTest('Completed current-environment gate unavailable')
        cls.directory = tempfile.TemporaryDirectory(); cls.root = Path(cls.directory.name)
        cls.plan, cls.inputs, cls.retained = p.prepare(cls.root)
        config = p.scan.load_config(p.b.base.d.CONFIG)
        elements, _ = load_elements(p.scan.resolve_srim_root(None, config)/'Data/ATOMDATA')
        cls.conditions = {(c.family, c.noble_percent): c for c in p.scan.generate_conditions(
            replace(config, noble_percent=p.FRACTIONS, pressures_atm=(1.,)), elements)}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_exact_grid_independence_and_accounting(self):
        cases = self.plan['cases']
        self.assertEqual(len(cases), 1260); self.assertEqual(len({c['point'] for c in cases}), 315)
        self.assertEqual({c['noble_percent'] for c in cases}, set(p.FRACTIONS))
        self.assertEqual({c['energy_kev'] for c in cases}, set(p.ENERGIES))
        self.assertEqual({c['family'] for c in cases}, {'Ar', 'Kr', 'Xe'})
        self.assertEqual(len({r['seed'] for r in self.retained+cases}), 2304)
        forbidden, _ = p.known_seeds(self.retained)
        self.assertFalse(forbidden & {c['seed'] for c in cases})
        self.assertTrue(all(c['pressure_atm'] == 1. and c['ions'] == 50 and not c['diagnostic'] for c in cases))
        self.assertEqual(self.plan['combined_points_after_stage'], 576)
        self.assertEqual(self.plan['complete_1atm_points_after_stage'], 504)
        self.assertEqual(self.plan['new_histories'], 63000)
        self.assertEqual(self.plan['anchor_input_gas_bridges'], 189)

    def test_low_energy_production_gate_fixed_and_disjoint(self):
        low = [c for c in self.plan['cases'] if c['phase'] == 'composition_gate']
        other = [c for c in self.plan['cases'] if c['phase'] == 'mixture_production']
        self.assertEqual(len(low), 45); self.assertEqual(len(other), 1215)
        self.assertTrue(all(c['energy_kev'] == 1. and c['repeat'] == 0 for c in low))
        self.assertEqual(len({(c['family'], c['ion'], c['noble_percent']) for c in low}), 45)
        self.assertFalse({c['name'] for c in low} & {c['name'] for c in other})
        self.assertEqual(self.plan['independent_batches_per_point'], 4)

    def test_all_inputs_density_stoichiometry_model_and_event_units(self):
        for case in self.plan['cases']:
            condition = self.conditions[case['family'], case['noble_percent']]
            p.validate_input(case, self.inputs[case['name']], condition)
            x = case['noble_percent']/100.; total = x+5*(1-x)
            self.assertAlmostEqual(case['atomic_fractions'][case['family']], x/total)
            self.assertAlmostEqual(case['atomic_fractions']['C'], (1-x)/total)
            self.assertAlmostEqual(case['atomic_fractions']['F'], 4*(1-x)/total)
            mass = x*condition.element_masses_u[case['family']]+(1-x)*(12.011+4*18.998)
            self.assertAlmostEqual(case['density_g_cm3'], mass/(.082057366080960*293.15*1000))
            self.assertAlmostEqual(sum(case['mass_fractions'].values()), 1.)
            self.assertEqual(case['origin_angstrom'], case['width_angstrom']/2)

    def test_only_intended_template_lines_change(self):
        for case in self.plan['cases'][::4]:
            original = Path(case['template_input']).read_text().splitlines()
            new = self.inputs[case['name']][0].decode().splitlines()
            allowed = {next(i for i, l in enumerate(original) if l.startswith('Cascades('))+1,
                       original.index('Target material : Number of Elements, Number of Layers')+1,
                       next(i for i, l in enumerate(original) if l.startswith('PlotType'))+1,
                       next(i for i, l in enumerate(original) if l.startswith('1 "'))}
            self.assertEqual(len(new), len(original))
            self.assertTrue(all(a == b for i, (a, b) in enumerate(zip(new, original)) if i not in allowed))

    def test_bad_input_species_density_mode_binding_and_event_rejected(self):
        case = self.plan['cases'][0]; contents = self.inputs[case['name']]
        condition = self.conditions[case['family'], case['noble_percent']]
        mutations = [contents[0].replace(b'Atom 1 = C =     6', b'Atom 1 = C =     9'),
                     contents[0].replace(b'1e-09', b'1e-08'),
                     contents[0].replace(f'5 {case["seed"]} 0'.encode(), f'2 {case["seed"]} 0'.encode()),
                     contents[0].replace(f'{case["density_g_cm3"]:.17g}'.encode(), b'0.001')]
        for content in mutations:
            changed = dict(case, input_sha256=hashlib.sha256(content).hexdigest())
            with self.assertRaises(ValueError):
                p.validate_input(changed, (content, contents[1]), condition)
        events = contents[1].replace(b'E0001', b'E9999')
        with self.assertRaisesRegex(ValueError, 'event'):
            p.validate_input(dict(case, trim_dat_sha256=hashlib.sha256(events).hexdigest()), (contents[0], events), condition)

    def test_central_geometry_is_maximum_adjacent_anchor_width(self):
        sources = {p.b.key(r): r for r in self.retained}
        for c in self.plan['cases'][::4]:
            anchors = (60, 80) if c['noble_percent'] < 80 else (80, 95)
            width = max(sources[c['family'], c['ion'], c['energy_kev'], n, 1.]['width_angstrom'] for n in anchors)
            self.assertEqual(c['width_angstrom'], width)

    def test_conservative_runtime_and_budget_drain(self):
        costs = {}
        for r in self.retained:
            costs.setdefault(p.b.key(r), []).append(r['wall_seconds'])
        for c in self.plan['cases'][::4]:
            anchors = (60, 80) if c['noble_percent'] < 80 else (80, 95)
            expected = max(sum(costs[c['family'], c['ion'], c['energy_kev'], n, 1.])/4 for n in anchors)
            self.assertEqual(c['estimated_seconds'], expected)
        f = self.plan['forecast']
        self.assertAlmostEqual(f['planning_hours'], 1.5*f['model_hours']+.5)
        self.assertLess(f['planning_hours'], 21.)
        self.assertLess(f['longest_estimated_batch_hours'], 2.5)
        self.assertEqual(self.plan['budget_hours'], 24)

    def test_live_manifest_pins_current_bytes_not_historical_substitution(self):
        self.assertEqual(self.plan['input_sha256'][str(p.history.LIVE)], p.history.NEW_SHA)
        self.assertEqual(self.plan['environment_review']['status'], 'AUDITED_CURRENT_ENVIRONMENT_GATE_PASSED')
        self.assertEqual(self.plan['environment_review']['completion']['new_independent_production_histories'], 0)

    def test_frozen_plan_cannot_change(self):
        file = self.root/'plan.json'; old = file.read_text()
        try:
            file.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen M3'):
                p.prepare(self.root)
        finally:
            file.write_text(old)

    def test_actual_composition_uses_printed_resolution_not_loose_relative_tolerance(self):
        case = self.plan['cases'][0]
        lines = [f'Layer # 1 - {case["condition_id"]}']
        for s in ('C', 'F', case['family']):
            lines.append(f'Layer # 1- {s} = {100*case["atomic_fractions"][s]:.1f} Atomic Percent = {100*case["mass_fractions"][s]:.2f} Mass Percent')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'TDATA.TXT'; path.write_text('\n'.join(lines)); output = {'TDATA.TXT': path}
            self.assertTrue(p.check_composition(case, output)['composition_parameters_match'])
            path.write_text('\n'.join(lines).replace('C = 14.6', 'C = 15.6'))
            with self.assertRaisesRegex(ValueError, 'atomic fraction'):
                p.check_composition(case, output)
            path.write_text('\n'.join(lines).replace(case['condition_id'], 'wrong_gas'))
            with self.assertRaisesRegex(ValueError, 'layer identity'):
                p.check_composition(case, output)

    def test_missing_duplicate_or_wrong_actual_target_species_rejected(self):
        case = self.plan['cases'][0]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'TDATA.TXT'; path.write_text('Layer # 1- C = 14.6 Atomic Percent = 7.41 Mass Percent')
            with self.assertRaisesRegex(ValueError, 'species'):
                p.check_composition(case, {'TDATA.TXT': path})

    def test_execution_failure_is_preserved_and_not_relabelled_complete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = root/'runtime'; runtime.mkdir(); attempt = root/'attempt'; attempt.mkdir()
            failed = dict(status='FAILED', name='example', attempt=str(attempt), artifact_sha256={}, error='containment failure')
            with patch.object(p, 'RUN', root), patch.object(p.b, 'run_case', return_value=failed):
                row = p.run_case({'name': 'example'}, (b'', b''), runtime, None, 0)
            self.assertEqual(row['status'], 'FAILED'); self.assertEqual(row['error'], 'containment failure')
            self.assertTrue((root/'cases/example/result.json').exists())

    def test_missing_completion_monitor_stops_completed_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); runtime = root/'runtime'; runtime.mkdir(); attempt = root/'attempt'; attempt.mkdir()
            row = dict(status='COMPLETE', name='example', attempt=str(attempt), artifact_sha256={})
            with patch.object(p, 'RUN', root), patch.object(p.b, 'run_case', return_value=row), \
                 patch.object(p.prior, 'check_density', return_value={}), patch.object(p, 'check_composition', return_value={}):
                result = p.run_case({'name': 'example'}, (b'', b''), runtime, None, 0)
            self.assertEqual(result['status'], 'FAILED')

    def test_current_environment_artifact_change_fails_closed(self):
        original = p.history.actual_sha256
        def changed(path):
            return 'changed' if Path(path) == p.history.LIVE else original(path)
        with patch.object(p.history, 'actual_sha256', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'Current Xvfb changed'):
                p.review_environment()

    def test_publish_partial_points_does_not_claim_precision_or_add_diagnostics(self):
        case = self.plan['cases'][0]
        row = dict(case, status='COMPLETE', ions_confirmed=50, stopped_ion_mean_dx_mm=.1, ionization_fraction=.2)
        with tempfile.TemporaryDirectory() as td, patch.object(p, 'RUN', Path(td)):
            state = {}; p.publish(state, [row], self.retained)
            points = json.loads((Path(td)/'points.json').read_text())
            added = next(x for x in points if x['point'] == case['point'])
            self.assertEqual(added['batches'], 1); self.assertIsNone(added['projected_range_mm_sem'])
            self.assertFalse(added['complete']); self.assertFalse(added['precision_met'])
            self.assertEqual(state['combined_points_complete'], 261)
            self.assertEqual(state['combined_completed_precision_flags'], 46)

    def exercise_supervisor(self, failure, used_seconds=0.):
        cases = [dict(name='low0', phase='composition_gate', estimated_seconds=2.),
                 dict(name='low1', phase='composition_gate', estimated_seconds=1.),
                 dict(name='production', phase='mixture_production', estimated_seconds=3.)]
        launched, snapshots = [], []
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td); group = root/p.UNIT; group.mkdir(); runtime = root/'runtime'; runtime.mkdir()
            (group/'memory.max').write_text(str(32*p.GIB)); (group/'memory.swap.max').write_text('0')
            (root/'budget.json').write_text(json.dumps({'used_seconds': used_seconds}))
            def job(case, *args):
                launched.append(case['name']); return dict(case, status='FAILED' if case['name'] == failure else 'COMPLETE')
            for owner, name, value in [(p, 'RUN', root), (p, 'WORKERS', 1)]:
                stack.enter_context(patch.object(owner, name, value))
            stack.enter_context(patch.object(p, 'resource_sample', return_value={'cgroup': str(group)}))
            stack.enter_context(patch.object(p.os, 'sched_getaffinity', return_value={0}))
            stack.enter_context(patch.object(p, 'memory_info', return_value={'MemAvailable': 32*p.GIB}))
            stack.enter_context(patch.object(p.signal, 'signal'))
            stack.enter_context(patch.object(p, 'prepare', return_value=({'cases': cases}, {c['name']: (b'', b'') for c in cases}, [])))
            stack.enter_context(patch.object(p.scan, 'prepare_runtimes', return_value=([runtime], {})))
            stack.enter_context(patch.object(p.scan, 'runtime_execution_context', return_value=None))
            stack.enter_context(patch.object(p.b.base, 'cached', return_value=None))
            stack.enter_context(patch.object(p, 'run_case', side_effect=job))
            stack.enter_context(patch.object(p, 'publish', side_effect=lambda state, *args: snapshots.append(copy.deepcopy(state))))
            p.scan.SHUTDOWN_REQUESTED.clear()
            try:
                result = p.execute()
            finally:
                p.scan.SHUTDOWN_REQUESTED.clear()
        return result, launched, snapshots

    def test_low_energy_integrity_failure_holds_all_remaining_production(self):
        code, launched, states = self.exercise_supervisor('low0')
        self.assertEqual(code, 1); self.assertEqual(launched, ['low0'])
        self.assertEqual(states[-1]['status'], 'NEEDS_ATTENTION')
        self.assertNotIn('mixture_production', {s['phase'] for s in states})

    def test_production_cannot_start_before_all_low_energy_checks_pass(self):
        code, launched, states = self.exercise_supervisor('production')
        self.assertEqual(launched, ['low0', 'low1', 'production']); self.assertEqual(code, 1)
        production = [s for s in states if s['phase'] == 'mixture_production']
        self.assertTrue(all(s['composition_gate_passed'] for s in production))
        self.assertEqual(states[-1]['status'], 'NEEDS_ATTENTION')

    def test_cumulative_budget_drain_does_not_reset_or_launch_new_jobs(self):
        code, launched, states = self.exercise_supervisor(None, used_seconds=p.BUDGET-p.JOB_TIMEOUT)
        self.assertEqual(launched, []); self.assertEqual(code, 1)
        self.assertEqual(states[-1]['status'], 'STOPPED')
        self.assertGreaterEqual(states[-1]['cumulative_hours'], (p.BUDGET-p.JOB_TIMEOUT)/3600)


if __name__ == '__main__':
    unittest.main()
