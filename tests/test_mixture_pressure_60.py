import copy
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts import mixture_pressure_60 as g


class MixturePressure60Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (g.previous.RUN/'completion.json').exists():
            raise unittest.SkipTest('M3 completion unavailable')
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        # A historical test uses the frozen predecessor registry, not plans
        # introduced by later dependent stages.
        if (g.RUN/'plan.json').exists():
            (cls.root/'plan.json').write_bytes((g.RUN/'plan.json').read_bytes())
        cls.plan, cls.inputs, cls.retained = g.prepare(cls.root)
        config = g.scan.load_config(g.b.base.d.CONFIG)
        elements, _ = g.load_elements(g.scan.resolve_srim_root(None, config)/'Data/ATOMDATA')
        cls.conditions = {(c.family, c.pressure_atm): c for c in g.scan.generate_conditions(
            replace(config, noble_percent=(60,), pressures_atm=g.PRESSURES), elements)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_grid_partition_and_seed_independence(self):
        cases = self.plan['cases']; existing = {g.b.key(x) for x in self.retained}
        self.assertEqual(len(cases), 1260); self.assertEqual(len({g.b.key(c) for c in cases}), 315)
        self.assertFalse(existing & {g.b.key(c) for c in cases})
        self.assertEqual(len(existing | {g.b.key(c) for c in cases}), 1134)
        self.assertEqual(len({x['seed'] for x in self.retained+cases}), 4536)
        forbidden, _ = g.seed_registry(self.root, self.retained)
        self.assertFalse(forbidden & {x['seed'] for x in cases})
        self.assertEqual(self.plan['forbidden_seed_count'], 5136)
        self.assertEqual(Counter(c['phase'] for c in cases), dict(low_pressure_gate=45, high_pressure_gate=45, pressure_production=1170))

    def test_all_inputs_parameters_density_events_and_column_depth(self):
        refs = {g.b.key(x): x for x in self.retained}
        for case in self.plan['cases']:
            condition = self.conditions[case['family'], case['pressure_atm']]
            g.p.validate_input(case, self.inputs[case['name']], condition)
            key = g.b.key(case); source = refs[(*key[:4], 1.)]
            self.assertAlmostEqual(case['density_g_cm3']/float(g.p.prior.layer_fields((Path(source['attempt'])/'TRIM.IN').read_bytes())[3]), case['pressure_atm'], places=11)
            self.assertGreaterEqual(case['width_angstrom']*case['pressure_atm']+1e-5, source['width_angstrom'])
            self.assertEqual(case['origin_angstrom'], case['width_angstrom']/2)
            self.assertFalse(case['diagnostic'])

    def test_only_intended_template_lines_change(self):
        for case in self.plan['cases'][::31]:
            old = Path(case['template_input']).read_text().splitlines()
            new = self.inputs[case['name']][0].decode().splitlines()
            allowed = {next(i for i, x in enumerate(old) if x.startswith('1 "'))}
            for prefix in ('Cascades(', 'PlotType', 'Target material : Number of Elements, Number of Layers'):
                allowed.add(next(i for i, x in enumerate(old) if x.startswith(prefix))+1)
            self.assertEqual(len(old), len(new))
            self.assertTrue({i for i, pair in enumerate(zip(old, new)) if pair[0] != pair[1]} <= allowed)

    def test_budget_and_conservative_timing(self):
        forecast = self.plan['forecast']
        self.assertLess(forecast['planning_hours']+3.025, 24)
        self.assertLess(forecast['longest_estimated_batch_hours'], 2.5)
        self.assertAlmostEqual(sum(forecast['phase_model_hours'].values()), forecast['model_hours'])
        self.assertEqual(self.plan['previous_review']['precision_flags'], 141)
        self.assertEqual(self.plan['previous_review']['independent_batches'], 3276)

    def test_measured_same_energy_pressure_timing_with_floor(self):
        means = {('Kr', 'F', 100., 80, q): 100*q for q in (.5, 1., 2.)}
        means['Kr', 'F', 100., 60, 1.] = 250.
        self.assertEqual(g.batch_cost(('Kr', 'F', 100., 60, .5), means), 250.)
        self.assertEqual(g.batch_cost(('Kr', 'F', 100., 60, 2.), means), 500.)
        with self.assertRaisesRegex(ValueError, 'timing condition'):
            g.batch_cost(('Kr', 'F', 100., 65, 2.), means)

    def test_schedule_agrees_with_frozen_three_phase_forecast(self):
        from scripts import bulk_campaign_schedule as schedule
        from collections import defaultdict
        import statistics
        grouped = defaultdict(list)
        for row in self.retained:
            grouped[g.b.key(row)].append(row['wall_seconds'])
        means = {k: statistics.mean(v) for k, v in grouped.items()}
        keys = {g.b.key(c) for c in self.plan['cases']}
        result = schedule.g5_estimate(keys, 4, means)
        self.assertEqual(result['model_hours'], round(self.plan['forecast']['model_hours'], 2))
        self.assertEqual(result['planning_hours'], round(self.plan['forecast']['planning_hours'], 2))
        self.assertEqual(result['batches'], 1260)
        for noble in (65, 70, 75, 85, 90, 95):
            other = {(f, ion, e, noble, pressure) for f, ion, e, _, pressure in keys}
            forecast = schedule.g5_estimate(other, 4, means)
            self.assertLess(forecast['planning_hours']+3.025, 24)
        with self.assertRaises(ValueError):
            schedule.g5_estimate(set(), 4, means)

    def test_geometry_does_not_shrink_any_audited_areal_envelope(self):
        for case in self.plan['cases'][::4]:
            envelope = max(r['width_angstrom']*r['pressure_atm'] for r in self.retained
                           if g.b.key(r)[:3] == g.b.key(case)[:3])
            self.assertGreaterEqual(case['width_angstrom']*case['pressure_atm']+1e-4, envelope)

    def test_noreset_gate_rejects_wrong_lifecycle(self):
        with patch.object(g, 'original_audit'):
            for metadata in ({}, {'reset_on_last_client': True}):
                with self.assertRaisesRegex(ValueError, 'noreset'):
                    g.audit_case({}, {'attempt': str(g.RUN/'cases'/'fake'), 'display_startup': metadata}, ())

    def test_invalid_density_or_binding_rejected(self):
        case = self.plan['cases'][0]; content, events = self.inputs[case['name']]
        condition = self.conditions[case['family'], case['pressure_atm']]
        wrong = copy.deepcopy(case); wrong['input_sha256'] = 'bad'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            g.p.validate_input(wrong, (content, events), condition)
        with self.assertRaisesRegex(ValueError, 'condition'):
            g.make_input(content, replace(condition, noble_percent=75), 'bad', 123, case['ion_z'], case['energy_kev'], 10)

    def test_cached_seed_registry_ignores_future_plans(self):
        before, paths = g.seed_registry(self.root, self.retained)
        with patch.object(Path, 'glob', side_effect=AssertionError('Must use frozen predecessor list')):
            after, after_paths = g.seed_registry(self.root, self.retained)
        self.assertEqual(before, after); self.assertEqual(paths, after_paths)

    def test_live_supervisor_bindings_prepare_same_plan(self):
        with patch.multiple(g.p, RUN=g.RUN, audit_case=g.audit_case, check_composition=g.gate.retry.check_composition), \
                patch.object(g.b, 'RUN', g.RUN), patch.object(g.b.base, 'RUN', g.RUN), \
                patch.object(g.scan, 'launch_command', g.gate.launch):
            plan, inputs, retained = g.prepare(self.root)
        self.assertEqual(plan, self.plan); self.assertEqual(inputs, self.inputs); self.assertEqual(retained, self.retained)

    def test_partial_points_keep_preceding_precision_flags(self):
        case = self.plan['cases'][0]
        row = dict(case, status='COMPLETE', ions_confirmed=50, stopped_ion_mean_dx_mm=.1, ionization_fraction=.2)
        with tempfile.TemporaryDirectory() as td, patch.object(g, 'RUN', Path(td)):
            state = {}; g.publish(state, [row], self.retained)
            self.assertEqual(state['combined_points_complete'], 819)
            self.assertEqual(state['completed_precision_flags'], 141)
            self.assertEqual(state['new_points_complete'], 0)

    def test_pressure_screen_fixed_family_and_zero_variance(self):
        rows = []
        for family in ('Ar', 'Kr', 'Xe'):
            for ion in ('C', 'F', family):
                for energy in g.p.ENERGIES:
                    for pressure in g.PRESSURES:
                        for repeat in range(4):
                            rows.append(dict(family=family, ion=ion, energy_kev=energy, noble_percent=60, pressure_atm=pressure,
                                             repeat=repeat, status='COMPLETE', ions_confirmed=50, model_id=g.MODEL, seed=len(rows)+1,
                                             ionization_fraction=.5, stopped_ion_mean_dx_mm=1/pressure))
        checks = g.pressure_checks(rows, self.plan['pressure_screen'])
        self.assertEqual(len(checks), 630)
        self.assertTrue(all(x['classification'] == 'inconclusive_zero_variance' for x in checks))
        wrong = dict(self.plan['pressure_screen'], comparisons=36)
        with self.assertRaisesRegex(ValueError, 'Prespecified'):
            g.pressure_checks(rows, wrong)
        with self.assertRaisesRegex(ValueError, 'Prespecified'):
            g.pressure_checks(rows, dict(self.plan['pressure_screen'], noble_percent=80))
        rows[-1]['seed'] = rows[0]['seed']
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            g.pressure_checks(rows, self.plan['pressure_screen'])


class PressureMathTests(unittest.TestCase):
    def test_known_equivalence_and_outside_margin_signals(self):
        spec = dict(noble_percent=60, comparisons=630, familywise_alpha=.05,
                    ionization_margin=[.95, 1.05], pressure_scaled_displacement_margin=[.9, 1.1])
        rows = []
        for family in ('Ar', 'Kr', 'Xe'):
            for ion in ('C', 'F', family):
                for energy in g.p.ENERGIES:
                    for pressure in g.PRESSURES:
                        for repeat in range(4):
                            perturb = 1+repeat*.0001
                            rows.append(dict(family=family, ion=ion, energy_kev=energy, noble_percent=60,
                                             pressure_atm=pressure, repeat=repeat, status='COMPLETE',
                                             ions_confirmed=50, model_id=g.MODEL, seed=len(rows)+1,
                                             ionization_fraction=.5*perturb,
                                             stopped_ion_mean_dx_mm=perturb/pressure))
        checks = g.pressure_checks(rows, spec)
        self.assertEqual(Counter(x['classification'] for x in checks), {'within_working_margin': 630})
        for row in rows:
            if row['pressure_atm'] != 1.:
                row['ionization_fraction'] *= 1.2
        checks = g.pressure_checks(rows, spec)
        self.assertEqual(Counter(x['classification'] for x in checks),
                         {'within_working_margin': 315, 'followup': 315})

    def test_nine_page_report_smoke_without_scientific_outputs(self):
        points = [dict(family=f, ion=ion, energy_kev=e, noble_percent=60, pressure_atm=q,
                       projected_range_mm=1/q, projected_range_mm_sem=.01/q,
                       ionization_fraction=.5, ionization_fraction_sem=.001)
                  for f in ('Ar', 'Kr', 'Xe') for ion in ('C', 'F', f)
                  for e in g.p.ENERGIES for q in g.PRESSURES]
        with tempfile.TemporaryDirectory() as td, patch.object(g, 'RUN', Path(td)):
            g.plots(points)
            self.assertGreater((Path(td)/'pressure.pdf').stat().st_size, 10000)


class SupervisorTests(unittest.TestCase):
    def exercise(self, failure, used=0):
        cases = [dict(name=phase, phase=phase, estimated_seconds=1.) for phase in g.PHASES]
        launched, snapshots = [], []
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td); group = root/g.UNIT; group.mkdir(); runtime = root/'runtime'; runtime.mkdir()
            (group/'memory.max').write_text(str(32*g.GIB)); (group/'memory.swap.max').write_text('0')
            (root/'budget.json').write_text(json.dumps(dict(used_seconds=used)))
            def job(case, *args):
                launched.append(case['name']); return dict(case, status='FAILED' if case['name'] == failure else 'COMPLETE')
            stack.enter_context(patch.object(g, 'RUN', root)); stack.enter_context(patch.object(g, 'WORKERS', 1))
            stack.enter_context(patch.object(g, 'resource_sample', return_value={'cgroup': str(group)}))
            stack.enter_context(patch.object(g.os, 'sched_getaffinity', return_value={0}))
            stack.enter_context(patch.object(g, 'memory_info', return_value={'MemAvailable': 32*g.GIB}))
            stack.enter_context(patch.object(g.signal, 'signal'))
            stack.enter_context(patch.object(g, 'prepare', return_value=({'cases': cases}, {c['name']: (b'', b'') for c in cases}, [])))
            stack.enter_context(patch.object(g.scan, 'prepare_runtimes', return_value=([runtime], {})))
            stack.enter_context(patch.object(g.scan, 'runtime_execution_context', return_value=None))
            stack.enter_context(patch.object(g.b.base, 'cached', return_value=None))
            stack.enter_context(patch.object(g.p, 'run_case', side_effect=job))
            stack.enter_context(patch.object(g, 'publish', side_effect=lambda state, *args: snapshots.append(copy.deepcopy(state))))
            g.scan.SHUTDOWN_REQUESTED.clear()
            try:
                code = g.execute()
            finally:
                g.scan.SHUTDOWN_REQUESTED.clear()
        return code, launched, snapshots

    def test_low_failure_blocks_high_and_production(self):
        code, launched, states = self.exercise('low_pressure_gate')
        self.assertEqual(code, 1); self.assertEqual(launched, ['low_pressure_gate'])
        self.assertEqual(states[-1]['status'], 'NEEDS_ATTENTION')

    def test_high_failure_blocks_production(self):
        code, launched, states = self.exercise('high_pressure_gate')
        self.assertEqual(code, 1); self.assertEqual(launched, ['low_pressure_gate', 'high_pressure_gate'])
        self.assertTrue(states[-1]['low_pressure_gate_passed'])

    def test_successful_canaries_required_before_production(self):
        code, launched, states = self.exercise('pressure_production')
        self.assertEqual(launched, list(g.PHASES)); self.assertEqual(code, 1)
        self.assertTrue(states[-1]['high_pressure_gate_passed'])

    def test_budget_drain_keeps_elapsed_time(self):
        code, launched, states = self.exercise(None, g.BUDGET-g.JOB_TIMEOUT)
        self.assertEqual(launched, []); self.assertEqual(code, 1)
        self.assertEqual(states[-1]['status'], 'STOPPED')
        self.assertGreaterEqual(states[-1]['cumulative_hours'], 21)


if __name__ == '__main__':
    unittest.main()
